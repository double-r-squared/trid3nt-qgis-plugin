# TRID3NT mesh environment -- the box OceanMesh2D and FloPy live in.
#
# This image is an ENVIRONMENT, not a worker: it carries the CHLNDDEV
# `oceanmesh` port, FloPy, and the two executables the FloPy meshers drive
# (triangle, gridgen) and nothing of ours. The mesh recipe path
# (trid3nt_server/tools/mesh/meshers/) bind-mounts its driver at
# /drivers and a rundir at /data, overrides the entrypoint with `python`, and
# runs the driver here; the libraries are the only things this image supplies.
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
# This layer (and every one through oceanmesh) is cache-critical: keep its
# inputs identical so the CGAL compile below does not re-run on a mesh-box bump.
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

# FloPy (MIT) composes the triangle / voronoi / gridgen meshers IN THE DRIVER,
# and pyproj gives the planar grid its CRS words. pyshp is how Gridgen writes
# its refinement/active-domain features as shapefiles (flopy's optional dep).
# Pinned exactly as the host venv pins them: a version skew here is a
# deliberate, IaC-visible bump.
ARG FLOPY_VERSION=3.10.0
ARG PYPROJ_VERSION=3.7.2
ARG PYSHP_VERSION=3.1.4
RUN pip install --no-cache-dir "flopy==${FLOPY_VERSION}" \
    "pyproj==${PYPROJ_VERSION}" "pyshp==${PYSHP_VERSION}"

# triangle + gridgen: two of the 28 binaries in the MODFLOW-ORG executables
# release, sha256-pinned, extracted ALONE into a bin dir the runtime copies -
# the mesh box takes only what its meshers drive. stdlib zipfile and urllib
# because slim has neither unzip nor curl.
ARG EXEC_RELEASE=29.0
ARG EXEC_SHA256=ab17192a3531dec61d4f4eab3da10f618e85d1f792d5edc71306a3e06e0e22c3
RUN mkdir -p /opt/mesh/bin \
    && python -c "\
import urllib.request;\
urllib.request.urlretrieve('https://github.com/MODFLOW-ORG/executables/releases/download/${EXEC_RELEASE}/linux.zip', '/tmp/linux.zip')" \
    && echo "${EXEC_SHA256}  /tmp/linux.zip" | sha256sum -c - \
    && python -c "\
import zipfile;\
z = zipfile.ZipFile('/tmp/linux.zip');\
names = [n for n in z.namelist() if n.rsplit('/', 1)[-1] in ('triangle', 'gridgen')];\
assert len(names) == 2, names;\
[open('/opt/mesh/bin/' + n.rsplit('/', 1)[-1], 'wb').write(z.read(n)) for n in names]" \
    && chmod +x /opt/mesh/bin/triangle /opt/mesh/bin/gridgen \
    && rm -f /tmp/linux.zip

# --------------------------------------------------------------------------- #
# Stage 2: slim runtime -- the venv and nothing else.
# --------------------------------------------------------------------------- #
FROM python:3.11-slim AS runtime
LABEL org.opencontainers.image.title="trid3nt-mesh" \
      org.opencontainers.image.description="The GPL-isolated mesh environment: the CHLNDDEV oceanmesh port plus FloPy with triangle and gridgen on python 3.11, run with a driver and a rundir bind-mounted in." \
      org.opencontainers.image.licenses="GPL-3.0-or-later"

# Runtime shared libs the CGAL-linked oceanmesh C++ extensions dlopen (GMP +
# MPFR; CGAL is header-only). The GDAL/GEOS/PROJ natives ride inside the
# rasterio/fiona/shapely manylinux wheels -- but the fiona wheel dlopen()s
# libexpat, so libexpat1 is explicitly added.
RUN apt-get update && apt-get install -y --no-install-recommends \
      libgmp10 libmpfr6 libexpat1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /opt/mesh/.venv /opt/mesh/.venv
COPY --from=build /opt/mesh/bin/ /usr/local/bin/
ENV PATH="/opt/mesh/.venv/bin:$PATH" \
    OCEANMESH_INPOLY_ACCEL=1

# Build-time smoke: both executables answer and the libraries import, so a pin
# or extract mistake surfaces at BUILD time instead of mid-build. Each help
# path exits 0 on its own; the grep is what proves the RIGHT binary ran.
RUN triangle -h 2>&1 | grep -qi "delaunay" \
    && gridgen 2>&1 | grep -q "GRIDGEN Version" \
    && python -c "import flopy, pyproj, shapely, shapefile" \
    && echo "mesh environment OK: triangle + gridgen + flopy present"
