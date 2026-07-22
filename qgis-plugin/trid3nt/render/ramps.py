"""Colormap-name -> gradient-stop table for QGIS-native raster rendering.

The TiTiler -> QGIS-native swap: the server now publishes rasters as raw
``s3://`` COG uris plus an explicit ``legend`` (colormap name + vmin/vmax),
and the PLUGIN owns colorization (``layers._apply_raster_renderer``). This
module is the pure-python colormap table that backs it -- no qgis imports,
so the table is testable in the stdlib test venv.

The names mirror the SERVER's TiTiler style registry
(``server/src/grace2_agent/tools/publish_layer.py`` --
``_TITILER_STYLE_REGISTRY`` values, the family rules, and the literal
``colormap_name=`` emissions). SYNC NOTE: when the server registry gains a
new colormap name, add it to ``SERVER_COLORMAP_NAMES`` + ``_RAMP_STOPS``
here; ``tests/test_raster_render.py`` scans the server file and fails until
the two lists agree, so the drift is never silent.

Stops are 5-point hex approximations of the matplotlib / ColorBrewer ramps
rio-tiler uses (lowercase rio-tiler naming). They are the FALLBACK when the
QGIS default style has no matching built-in gradient -- close enough that a
flood-depth raster reads as the same ramp family as the web render, and
never silently grey.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

#: Every colormap name the SERVER style registry can emit (see the module
#: docstring sync note). ``viridis`` is also the server's safe default + the
#: percentile-fallback ramp.
SERVER_COLORMAP_NAMES: Tuple[str, ...] = (
    "blues",
    "gnbu",
    "gray",
    "gray_r",
    "greens",
    "hsv",
    "inferno",
    "magma",
    "oranges",
    "rdbu",
    "rdylbu_r",
    "rdylgn",
    "rdylgn_r",
    "reds",
    "viridis",
    "ylgn",
    "ylgnbu",
    "ylorrd",
)

#: rio-tiler colormap name -> 5 hex stops at t = 0, 0.25, 0.5, 0.75, 1.
#: ``terrain`` is a DEFENSIVE extra: it is not in the current server
#: registry, but legacy persisted tile-template uris can carry it
#: (pre-registry DEM publishes), and a legacy layer must never fall back to
#: grey just because the name predates the table.
_RAMP_STOPS: dict[str, Tuple[str, ...]] = {
    "viridis": ("#440154", "#3b528b", "#21918c", "#5ec962", "#fde725"),
    "magma": ("#000004", "#51127c", "#b73779", "#fc8961", "#fcfdbf"),
    "inferno": ("#000004", "#57106e", "#bc3754", "#f98e09", "#fcffa4"),
    "blues": ("#f7fbff", "#c6dbef", "#6baed6", "#2171b5", "#08306b"),
    "greens": ("#f7fcf5", "#c7e9c0", "#74c476", "#238b45", "#00441b"),
    "oranges": ("#fff5eb", "#fdd0a2", "#fd8d3c", "#d94801", "#7f2704"),
    "reds": ("#fff5f0", "#fcbba1", "#fb6a4a", "#cb181d", "#67000d"),
    "rdbu": ("#67001f", "#f4a582", "#f7f7f7", "#92c5de", "#053061"),
    "rdylbu_r": ("#313695", "#74add1", "#ffffbf", "#fdae61", "#a50026"),
    "rdylgn": ("#a50026", "#fdae61", "#ffffbf", "#a6d96a", "#006837"),
    "rdylgn_r": ("#006837", "#a6d96a", "#ffffbf", "#fdae61", "#a50026"),
    "ylgn": ("#ffffe5", "#d9f0a3", "#78c679", "#238443", "#004529"),
    "ylgnbu": ("#ffffd9", "#c7e9b4", "#41b6c4", "#225ea8", "#081d58"),
    "ylorrd": ("#ffffcc", "#fed976", "#fd8d3c", "#e31a1c", "#800026"),
    "gnbu": ("#f7fcf0", "#ccebc5", "#7bccc4", "#2b8cbe", "#084081"),
    "gray": ("#000000", "#404040", "#808080", "#bfbfbf", "#ffffff"),
    "gray_r": ("#ffffff", "#bfbfbf", "#808080", "#404040", "#000000"),
    # Cyclic hue wheel (compass aspect) -- starts AND ends on red.
    "hsv": ("#ff0000", "#b3ff00", "#00ffff", "#4d00ff", "#ff0000"),
    "terrain": ("#333399", "#00cc66", "#ffff99", "#806050", "#ffffff"),
}

#: rio-tiler name -> (QGIS default-style gradient preset name, invert).
#: Consulted FIRST (``layers`` samples the built-in ramp when the installed
#: QGIS style carries it); ``_RAMP_STOPS`` is the always-available fallback.
#: ``gray`` inverts Greys because ColorBrewer Greys runs white->black while
#: matplotlib/rio-tiler ``gray`` runs black->white. ``hsv``/``terrain`` have
#: no QGIS preset -- stops only.
QGIS_RAMP_SOURCES: dict[str, Tuple[str, bool]] = {
    "viridis": ("Viridis", False),
    "magma": ("Magma", False),
    "inferno": ("Inferno", False),
    "blues": ("Blues", False),
    "greens": ("Greens", False),
    "oranges": ("Oranges", False),
    "reds": ("Reds", False),
    "rdbu": ("RdBu", False),
    "rdylbu_r": ("RdYlBu", True),
    "rdylgn": ("RdYlGn", False),
    "rdylgn_r": ("RdYlGn", True),
    "ylgn": ("YlGn", False),
    "ylgnbu": ("YlGnBu", False),
    "ylorrd": ("YlOrRd", False),
    "gnbu": ("GnBu", False),
    "gray": ("Greys", True),
    "gray_r": ("Greys", False),
}

#: The never-grey last resort: an unknown colormap name renders viridis (the
#: server's own safe default) instead of QGIS default grayscale.
DEFAULT_COLORMAP = "viridis"


def resolve_stops(name: Optional[str]) -> Optional[List[Tuple[float, str]]]:
    """``colormap name -> [(t, "#rrggbb"), ...]`` gradient stops, or None.

    Lowercases the name; a ``<base>_r`` name whose base IS in the table
    resolves to the base stops reversed (future server ``*_r`` variants keep
    working without a table edit). Returns ``None`` only for a name with no
    table entry at all -- callers degrade to ``DEFAULT_COLORMAP`` (with an
    honest note), never to grey.
    """
    key = (name or "").strip().lower()
    if not key:
        return None
    stops = _RAMP_STOPS.get(key)
    if stops is None and key.endswith("_r"):
        base = _RAMP_STOPS.get(key[: -len("_r")])
        if base is not None:
            stops = tuple(reversed(base))
    if stops is None:
        return None
    n = len(stops)
    return [(i / (n - 1), color) for i, color in enumerate(stops)]
