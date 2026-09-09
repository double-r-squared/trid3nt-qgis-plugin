"""Generic data-router engine: a declared source spec becomes a fetch tool.

A helper package, not a tool. Importing it is side-effect-free -- it walks no
tree and registers nothing; a caller triggers registration explicitly through
``registration.register_specs_from_tree()``."""

from __future__ import annotations

from . import errors, router, spec

__all__ = ["errors", "router", "spec"]
