# TRID3NT TELEMAC-2D river-dye LOCAL worker (PHASE 2 / river-dye North Star).
#
# Runtime image for the ``telemac_river_dye`` archetype under the local-docker
# solve backend (the SFINCS/GeoClaw analogue). The agent-side launcher
# bind-mounts a rundir at /data (``docker run ... -v <rundir>:/data -w /data``)
# carrying manifest.json; the entrypoint runs the PROVEN P1 pipeline
# (``telemac_river_dye_build``: real NHDPlus reach -> Gmsh channel mesh ->
# Copernicus DEM bed -> TELEMAC-2D TRACER solve) and writes the mesh + result
# ``.slf`` + ``telemac_metrics.json`` back into /data. The agent supervisor
# uploads /data -> s3://<runs>/<run_id>/ and writes completion.json (so this
# image does NO object-store I/O and needs NO boto3 -- keeping a heavy
# conda/TELEMAC image as lean as honestly possible).
#
# ENGINE + PIN DISCIPLINE (the P1 findings, non-negotiable):
#   * opentelemac v9.0.0 (conda-forge, GPL-3): ships a PRECOMPILED telemac2d
#     binary + the SELAFIN Python tooling; the conda activation sets
#     $HOMETEL/$SYSTELCFG/$USETELCFG + puts telemac2d.py on PATH.
#   * numpy==1.26.4 (MANDATORY): numpy 2.x breaks TELEMAC's _hermes C ext.
#   * rasterio==1.3.11 (<1.4): numpy-2/GDAL ABI safety (self-contained manylinux
#     wheel; no conda GDAL to conflict).
#   * gmsh==4.15.2 via the PyPI WHEEL (NOT conda-forge gmsh, which pulls a CUDA
#     viskores closure that balloons + corrupts the image -- the P1 finding).
#   * pystac-client / planetary-computer for the Copernicus GLO-30 DEM fetch;
#     pyproj / shapely / scipy for the mesh + centerline geometry.
# All versions are the EXACT set the P1 pipeline proved on a real Snake River
# dye solve (a version skew here is an IaC-visible, deliberate bump).
#
# Build context = THE WORKER DIR ITSELF, not the repo root -- the
# container-hygiene rule (a .dockerignore trims tests/pyc). So the COPY path is
# ``.`` relative to this dir:
#   docker build -t trid3nt-local/telemac:latest services/workers/telemac/

FROM mambaorg/micromamba:2.0.5-debian12-slim AS runtime

LABEL org.opencontainers.image.title="trid3nt-telemac-river-dye-worker" \
      org.opencontainers.image.description="TRID3NT TELEMAC-2D river-dye surface-tracer local worker: manifest -> NHDPlus reach -> Gmsh channel mesh -> Copernicus DEM bed -> TELEMAC-2D TRACER solve -> result .slf + telemac_metrics.json in the mounted rundir." \
      org.opencontainers.image.licenses="GPL-3.0"

# --- runtime shared libs the PyPI gmsh wheel dlopens at IMPORT (headless) -----
# The gmsh wheel links libGLU/libGL + a handful of X libs even for pure
# mesh-generation (no GUI); the debian12-slim base lacks them, so ``import
# gmsh`` fails with ``libGLU.so.1: cannot open shared object file``. Install the
# minimal set (root); --no-install-recommends + apt-list cleanup for hygiene.
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libglu1-mesa \
        libgl1 \
        libxrender1 \
        libxcursor1 \
        libxinerama1 \
        libxft2 \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Back to the non-root env user for the conda/pip installs (mambaorg default).
USER $MAMBA_USER

# --- pinned engine + geo stack (conda for opentelemac + the numpy ABI pin) ---
# opentelemac carries its own numpy; we pin numpy==1.26.4 in the SAME solve so
# the _hermes C ext stays ABI-valid. pip (below) then installs the geo deps into
# the same env WITHOUT upgrading numpy.
ARG OPENTELEMAC_VERSION=v9.0.0
ARG NUMPY_VERSION=1.26.4
RUN micromamba install -y -n base -c conda-forge \
        "opentelemac=${OPENTELEMAC_VERSION}" \
        "numpy=${NUMPY_VERSION}" \
        pip \
    && micromamba clean --all --yes

# Activate the base env for every subsequent RUN (so $HOMETEL/$SYSTELCFG and
# telemac2d.py resolve, and pip targets the conda env's python).
ARG MAMBA_DOCKERFILE_ACTIVATE=1

# --- geo deps via PyPI (the P1-proven wheel set; numpy pinned so pip cannot
# pull it to 2.x, which would break TELEMAC's _hermes ext) ---
RUN python -m pip install --no-cache-dir \
        "numpy==1.26.4" \
        "gmsh==4.15.2" \
        "rasterio==1.3.11" \
        "shapely==2.1.2" \
        "pyproj==3.7.2" \
        "scipy==1.17.1" \
        "pystac-client==0.9.0" \
        "planetary-computer==1.0.0" \
    && python -c "import numpy,scipy,rasterio,shapely,pyproj,gmsh,pystac_client,planetary_computer; \
print('geo deps OK numpy', numpy.__version__, 'rasterio', rasterio.__version__, 'gmsh', gmsh.__version__)" \
    && python -c "import numpy; assert numpy.__version__=='1.26.4', 'numpy pin drifted: '+numpy.__version__" \
    && python -c "from data_manip.extraction.telemac_file import TelemacFile; print('TELEMAC _hermes import OK')"

# --- the worker package (entrypoint + the P1 payload) -----------------------
# Build context is THIS worker dir; ``.`` copies entrypoint.py +
# telemac_river_dye_build.py + __init__.py (tests/pyc excluded via
# .dockerignore). The entrypoint imports its sibling ``telemac_river_dye_build``
# off the script dir, so both land in the same directory.
COPY --chown=root:root . /opt/trid3nt/services/workers/telemac/

# Run as root so the container can write the mounted rundir (bind-mounted /data
# is owned by the host launcher user) -- the same effective behavior as the
# deltares/sfincs-cpu bind-mount worker. The conda base env stays active.
USER root

ENV PYTHONUNBUFFERED=1 \
    TRID3NT_TELEMAC_DATA_DIR=/data \
    OMPI_ALLOW_RUN_AS_ROOT=1 \
    OMPI_ALLOW_RUN_AS_ROOT_CONFIRM=1
# opentelemac launches the solver via ``mpirun --use-hwthread-cpus -np 1``;
# OpenMPI's prterun REFUSES to run as root (we run root so the bind-mounted
# /data is writable, mirroring deltares/sfincs). These two env vars are
# OpenMPI's sanctioned override for a single-rank containerized root solve.

WORKDIR /data

# Build-time smoke: the worker payload + entrypoint import cleanly under the
# activated env, and a ReachConfig maps from a synthetic manifest override (no
# network / no solve). Catches a path/import/pin mistake at BUILD time.
RUN python -c "import sys; sys.path.insert(0, '/opt/trid3nt/services/workers/telemac'); \
import telemac_river_dye_build as B; import entrypoint as E; \
cfg = E._reach_config(__import__('pathlib').Path('/data'), {'distance_km': 4.0, 'bogus_key': 1}); \
assert cfg.distance_km == 4.0 and cfg.workdir == '/data'; \
print('telemac worker import + ReachConfig map OK:', cfg.name)"

# One-shot solver: the activation entrypoint (_entrypoint.sh) sources the base
# env (HOMETEL/SYSTELCFG/PATH -> telemac2d.py), then execs the worker. CMD is
# the default argv; the launcher appends nothing (telemac_args=[]) so the
# entrypoint reads /data/manifest.json and writes outputs into /data.
ENTRYPOINT ["/usr/local/bin/_entrypoint.sh"]
CMD ["python", "/opt/trid3nt/services/workers/telemac/entrypoint.py"]
