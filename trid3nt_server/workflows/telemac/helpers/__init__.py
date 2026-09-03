"""The door. Every consumer imports the module it means, never this file.

An aggregating surface here would re-export names whose real home is a module,
and a reader chasing a name would land on a list instead of on the code.
"""
