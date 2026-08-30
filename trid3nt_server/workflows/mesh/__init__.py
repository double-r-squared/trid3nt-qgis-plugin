"""Standalone mesh builder + the engine-agnostic mesh precondition gate.

Only the KIND vocabulary and the accept-set are re-exported here: a template's
``declarations.py`` reads them, and the router that would come with the rest of
the package imports the tool registry, which imports the templates.
"""

from trid3nt_server.workflows.mesh.kinds import Compatible, MeshKind

__all__ = ["Compatible", "MeshKind"]
