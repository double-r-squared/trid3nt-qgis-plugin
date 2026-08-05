"""HEC-RAS 2025 Beta headless-characterization worker (ADR 0127 spike).

The 2025 line is HEC's ground-up C#/.NET rewrite: a single-`.h5` project, a new
explicit solver, a NATIVE headless mesher (`ras mesh` / `ras prepare` compute the
subgrid property tables the 6.x M3 STOP could not build on Linux), and a
documented `ras` CLI (`createterrain -> mesh -> prepare -> solve -> map`).

SPIKE VERDICT (2026-08-04): NO-GO-YET. The managed .NET 9 CLI is Linux-portable
(it runs on `dotnet/runtime:9.0` and prints the full verb surface), but the
public release (hec-downloads 1.0.44, `HEC-RAS_2025_Beta.zip`) is a win-x64
self-contained publish -- ALL native compute/geospatial payload
(`RasNativeParallel`, `gdal_wrap`, `libhdf5`, `hecdss`) is Windows only. HEC has
not yet published a Linux/container build, so no headless SOLVE is possible.

This worker is the flip-ready host + the reproducible characterization probe, not
a registered engine. See README.md and docs/decisions/0127-hecras-2025-spike.md.
"""
