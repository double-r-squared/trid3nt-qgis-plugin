"""The in-container mesher drivers, mounted into the boxes their libraries live in.

Each file here runs INSIDE one image - ``mesh:latest`` for oceanmesh, ``telemac:
latest`` for telapy - and imports nothing from this package: the mount is the only
thing that connects them. They live in the product tree rather than in a sandbox
because the meshers beside them shell them on every build.

``drivers_dir()`` is the path a mesher mounts; the container never imports this
module.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["drivers_dir"]


def drivers_dir() -> Path:
    """The directory a mesher mounts into its box as ``/drivers``."""
    return Path(__file__).resolve().parent
