"""Data fetchers, one package per source, grouped by the phenomenon measured.

Shared cross-domain helpers live at this level; everything else is under a
domain subpackage."""

# A DATASET SOURCE IS A DECLARATION, NOT A MODULE. A source is a ``source.yaml``
# the shared router executes, and every surface it has - the model-facing
# docstring, the synthesized signature, the cache key, the confirm gate, the
# fallback ladder, the emitted layer - is built from that one file. What a
# declaration cannot say, a named hook beside it says, and the router calls it.
#
# A hand-rolled reader is a SECOND fetcher: it carries its own retry, its own
# error vocabulary and its own provenance, and none of the three is reachable by
# the guards that make a fetch honest. Adding a source adds a declaration.
