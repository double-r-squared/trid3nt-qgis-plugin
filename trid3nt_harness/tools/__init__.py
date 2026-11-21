"""Tool surfacing for the model: retrieval, the search tools and the dispatch-seam kwargs cleanup."""

# The registry populates before any module here runs: a surfacing module reads TOOL_REGISTRY at import and the
# registry's own import reaches back into this package.
from trid3nt import tools as _registry  # noqa: F401
