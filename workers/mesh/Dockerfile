# TRID3NT mesh worker -- oceanmesh coastal_tin generator (oceanmesh wave leg 2).
#
# A DEDICATED, GPL-ISOLATED mesh worker (the load-bearing decision below): the
# `oceanmesh` PyPI package is GPL-3, so -- exactly like gmsh in the TELEMAC image
# -- it must NEVER enter the server venv. It also carries three pybind11 C++
# extensions (delaunay_class / HamiltonJacobi / fast_geometry) that link CGAL
# (>=5.0) -> GMP + MPFR, so it needs a full C++ toolchain to build. Riding an
# existing worker would either drag CGAL into that image or fail the source
# build; a slim, single-purpose mesh image is the honest home (container-hygiene
# rule: multi-stage build, minimal runtime, size reported in ADR 0101).
#
# Backs the server-side agent/mesh/coastal_tin.py component: shoreline + DEM ->
# graded coastal TIN via OceanMesh2D sizing functions (distance / feature-size /
# wavelength / slope grading + gradation enforcement). NO solver consumes the
# TIN yet (future SCHISM/TELEMAC-coastal); this wave lands the generator + a
# paper-first validation case (oceanmesh's own shipped test geometry + the
# OceanMesh2D User Guide quality floor).
#
# Shoreline input: a vector polygon shapefile supplied per-run via the manifest
# (GSHHG L1 is the keyless source oceanmesh's README + tests use). The validation
# case uses oceanmesh's OWN bundled test fixtures so it needs no network.

# --------------------------------------------------------------------------- #
# Stage 1: build oceanmesh (+ its CGAL-linked C++ extensions) into a venv.
# --------------------------------------------------------------------------- #
FROM python:3.11-slim AS build

# CGAL + GMP + MPFR + Eigen + a C++ toolchain: oceanmesh's pyproject build-system
# compiles pybind11 modules against CGAL (header-only on Bookworm) which needs
# GMP/MPFR. GDAL/geos/proj -dev back rasterio/fiona/shapely source fallbacks.
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential cmake ca-certificates git \
      libcgal-dev libgmp-dev libmpfr-dev libeigen3-dev \
      libgdal-dev libgeos-dev libproj-dev \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/mesh/.venv
ENV PATH="/opt/mesh/.venv/bin:$PATH"
RUN pip install --no-cache-dir --upgrade pip setuptools wheel pybind11 "Cython>=0.29"

# oceanmesh (GPL-3) is NOT on PyPI -- install from the canonical CHLNDDEV repo,
# PINNED to the v1.0.0 release tag (reproducibility / container-hygiene rule).
# numpy<2 for the compiled-extension ABI safety class the TELEMAC worker holds.
ARG OCEANMESH_REF=v1.0.0
RUN pip install --no-cache-dir "numpy<2" \
    && pip install --no-cache-dir "git+https://github.com/CHLNDDEV/oceanmesh.git@${OCEANMESH_REF}" meshio

# --------------------------------------------------------------------------- #
# Stage 2: slim runtime -- the venv + the worker code only.
# --------------------------------------------------------------------------- #
FROM python:3.11-slim AS runtime
LABEL org.opencontainers.image.title="trid3nt-mesh-worker" \
      org.opencontainers.image.description="oceanmesh (GPL-3) coastal_tin generator: shoreline+DEM -> graded coastal TIN via OceanMesh2D sizing functions. Reads a manifest from /data, writes coastal_tin.geojson + mesh_stats.json." \
      org.opencontainers.image.licenses="GPL-3.0-or-later"

# Runtime shared libs the CGAL-linked oceanmesh C++ extensions dlopen (GMP +
# MPFR; CGAL is header-only). The GDAL/GEOS/PROJ natives ride inside the
# rasterio/fiona/shapely manylinux wheels -- but the fiona wheel dlopen()s
# libexpat (the documented modflow/rasterio lesson, ADR 0100), so libexpat1 is
# explicitly added.
RUN apt-get update && apt-get install -y --no-install-recommends \
      libgmp10 libmpfr6 libexpat1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /opt/mesh/.venv /opt/mesh/.venv
ENV PATH="/opt/mesh/.venv/bin:$PATH" \
    OCEANMESH_INPOLY_ACCEL=1

WORKDIR /app
COPY __init__.py entrypoint.py coastal_tin_build.py /app/

ENTRYPOINT ["python", "/app/entrypoint.py"]
