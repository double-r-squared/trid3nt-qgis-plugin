"""The MODFLOW authoring: the one simulation directory, the DISV beside it, and the checks.

``simulation`` writes ``mfsim.nam``, its timing and solution and one file per
package from a sheet; ``mesh_files`` writes the one file that is the accepted
mesh's, and never the two shall write it. ``validate`` and ``staged_check`` hold
the directory to the engine before it stages.
"""

from __future__ import annotations

__all__ = ["DISV_FILE", "MFSIM_FILE"]

#: The DISV bytes' name in a run directory; the model name file's PACKAGES block names it, and a staged check digests the file against the artifact the run was handed.
DISV_FILE = "mesh.disv"

#: The simulation name file - the steering file every MODFLOW run dispatches on.
MFSIM_FILE = "mfsim.nam"
