"""Generic data-router engine (the fetcher fold, phase-1 pilot).

Authority: ``docs/specs/router-pilot-contract.md`` + ``docs/specs/data-router-fold.md``.
Leading underscore = helper package, NOT a tool (matching ``_fetch_common.py`` /
``_pc_stac.py``). Importing this package is side-effect-free: it does NOT walk the
tree or register any virtual tool. The fold-arm / pilot lane triggers registration
explicitly via ``registration.register_specs_from_tree()``.

Surface:
  - ``spec``          : ``SourceSpec`` loader (tree walk + validation + corpus pickup)
  - ``router``        : the engine (validate -> gate -> dispatch -> cache -> LayerURI)
  - ``executors``     : raster_cog / vector_fgb / station_timeseries
  - ``transforms``    : tiled_mosaic / join
  - ``errors``        : Router* typed errors over the ``_fetch_common`` bases
  - ``registration``  : virtual-tool synthesis + the env-gated pool-substitution map
"""

from __future__ import annotations

from . import errors, router, spec

__all__ = ["errors", "router", "spec"]
