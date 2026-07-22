"""GRACE-2 COMBINED SFINCS coastal quadtree worker (BUILD + SOLVE, GPL-isolated).

> Repurposed from the deck-builder-only worker into the COMBINED build+solve
> worker. The package keeps its historical ``sfincs_deckbuilder`` name (wired
> into the Dockerfile module path + the agent's Batch-submit seam), but it now
> performs the FULL coastal job in ONE Batch job.

In a single AWS Batch job this worker:

  1. BUILDS a MULTI-LEVEL refined SFINCS *quadtree* + *SnapWave* deck from a
     build-spec JSON via Deltares ``cht_sfincs`` (GPL-3.0) — with auto-derived
     refinement (topobathy 0 m contour + nearshore band + slope + OSM rivers +
     OSM buildings), a quadtree cell-budget cap, and building flow-obstacles; and
  2. SOLVES that deck by invoking the upstream ``/usr/local/bin/sfincs`` binary
     (MIT, from the deltares/sfincs-cpu base image) IN-PROCESS on the LOCAL deck
     dir (no S3 round-trip), then writes ``sfincs_map.nc`` + a UNION
     ``completion.json`` the agent's ``wait_for_completion`` polls identically.

This collapses the former two Batch job-defs (a separate GPL deck-builder + a
separate MIT solve shim) into one job-def
(``GRACE2_AWS_BATCH_JOB_DEF_SFINCS_QUADTREE``): one submit, one poll, no deck
round-trip.

GPL boundary
------------
``cht_sfincs`` is GPL-3.0. It lives ONLY inside THIS worker's container image and
is imported ONLY by ``entrypoint.py`` (lazily, inside ``build_deck`` + the
refinement/obstacle helpers). The GRACE-2 agent venv and ALL agent code
(``services/agent/src/grace2_agent/**``) NEVER import ``cht_sfincs`` — the agent
reaches this worker arms-length over the object-store + AWS-Batch-submit seam, so
the GPL code stays fully isolated in its own image. The SFINCS solver binary is
MIT-licensed (shipped by the base image); the combined image's license is
therefore ``GPL-3.0-or-later AND MIT``.

Pure-Python helpers in ``entrypoint`` that do NOT touch ``cht_sfincs`` (manifest
parse, build-spec validation, S3 I/O, the time-column normalizer, the
cell-budget estimator + cap, the SFINCS-binary invocation) are unit-tested
without importing the GPL library; ``build_deck`` itself (incl. auto-refinement +
building obstacles) is exercised against the spike venv where ``cht_sfincs`` is
installed.
"""

__all__ = ["__version__"]

__version__ = "0.2.0"
