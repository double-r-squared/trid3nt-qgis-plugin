"""Standalone mesh builder + the engine-agnostic mesh precondition gate.

Nothing is re-exported here: the router that would come with the rest of the
package imports the tool registry, which imports the templates, so a template
reading a name off this package would close that loop. The KIND vocabulary is
imported from ``mesh.kinds`` by its own path, and the supply contract a template
declares lives with the rest of the declaration vocabulary in ``workflows.lib``.
"""
