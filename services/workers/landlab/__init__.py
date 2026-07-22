"""GRACE-2 Landlab AWS Batch worker package (sprint-17 — NEW engine).

A thin S3-IN -> build RasterModelGrid from a DEM COG -> run a documented Landlab
component chain (LandslideProbability / OverlandFlow) -> field COG -> S3-OUT
shim, mirroring the SWMM/MODFLOW workers. The Landlab numerics live in
``component_chain`` (lazy landlab import) so the entrypoint stays a transport
shim and the chain is unit-testable in isolation.
"""
