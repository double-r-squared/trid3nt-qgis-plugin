# TRID3NT mesh environment -- the box OceanMesh2D lives in.
#
# This image is an ENVIRONMENT, not a worker: it carries the CHLNDDEV
# `oceanmesh` port and nothing of ours. The mesh recipe path
# (trid3nt_server/workflows/mesh/meshers/om2d.py) bind-mounts its driver at
# /drivers and a rundir at /data, overrides the entrypoint with `python`, and
# runs the driver here; the library is the only thing this image supplies.
#
# GPL ISOLATION is why the box exists. `oceanmesh` is GPL-3, so -- exactly like
# gmsh in the TELEMAC image -- it must never enter the server venv. It also
# carries three pybind11 C++ extensions (delaunay_class / HamiltonJacobi /
# fast_geometry) that link CGAL (>=5.0) -> GMP + MPFR, so it needs a full C++
# toolchain to build: riding an existing image would drag CGAL into that image
# or fail the source build.
#
# Every input is bind-mounted per run: the GSHHG L1 polygon shapefile at
# /shoreline, the config and arrays at /data. The container is run with
# `--network none` and resolves nothing itself.

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
# Stage 2: slim runtime -- the venv and nothing else.
# --------------------------------------------------------------------------- #
FROM python:3.11-slim AS runtime
LABEL org.opencontainers.image.title="trid3nt-mesh" \
      org.opencontainers.image.description="The GPL-isolated OceanMesh2D environment: the CHLNDDEV oceanmesh port on python 3.11, run with a driver and a rundir bind-mounted in." \
      org.opencontainers.image.licenses="GPL-3.0-or-later"

# Runtime shared libs the CGAL-linked oceanmesh C++ extensions dlopen (GMP +
# MPFR; CGAL is header-only). The GDAL/GEOS/PROJ natives ride inside the
# rasterio/fiona/shapely manylinux wheels -- but the fiona wheel dlopen()s
# libexpat, so libexpat1 is explicitly added.
RUN apt-get update && apt-get install -y --no-install-recommends \
      libgmp10 libmpfr6 libexpat1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /opt/mesh/.venv /opt/mesh/.venv
ENV PATH="/opt/mesh/.venv/bin:$PATH" \
    OCEANMESH_INPOLY_ACCEL=1
