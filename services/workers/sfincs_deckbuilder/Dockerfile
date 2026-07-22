# COMBINED SFINCS coastal quadtree worker (BUILD + SOLVE in one image).
#
# Fuses the two former one-shot Batch workers into a single image + a single
# job-definition (TRID3NT_AWS_BATCH_JOB_DEF_SFINCS_QUADTREE):
#   1. BUILD — Deltares `cht_sfincs` (GPL-3.0, pinned commit) authors a refined
#      multi-level SFINCS quadtree + SnapWave deck from a build-spec JSON, with
#      auto-derived refinement, a cell budget cap, and building obstacles.
#   2. SOLVE — the upstream `/usr/local/bin/sfincs` binary (from the Deltares
#      sfincs-cpu image) runs that deck IN-PLACE in the same job (no S3 round-
#      trip), then the entrypoint uploads sfincs_map.nc + a UNION completion.json.
#
# GPL ISOLATION — load-bearing:
#   `cht_sfincs` is GPL-3.0 and lives ONLY in THIS image, imported ONLY by
#   `entrypoint.py` (lazily, inside build_deck + the refinement/obstacle helpers).
#   The agent venv and all agent code NEVER import `cht_sfincs`; the agent
#   reaches this worker arms-length over the object-store + AWS-Batch-submit seam.
#   The SFINCS solver binary itself is MIT-licensed (Deltares ships it in the
#   sfincs-cpu base). The combined image carries BOTH — the GPL cht venv overlaid
#   on the MIT solve base — but only the deck-build PATH touches GPL code; the
#   solve PATH is the same MIT binary the standalone solve worker invokes.
#
# BASE CHOICE (the combine's real packaging decision):
#   FROM deltares/sfincs-cpu (Ubuntu 22.04 Jammy) — this brings the SnapWave-
#   capable /usr/local/bin/sfincs binary the combined worker invokes in-process.
#   We then PIP-INSTALL the cht_sfincs closure into a venv ON TOP of that base.
#   The alternative (base on python:slim + COPY the sfincs binary across) is more
#   fragile: the binary links against the base image's OpenMP/Fortran runtime, so
#   keeping it on its native sfincs-cpu base is the honest choice.
#
# THE ONE BUILD-TIME RISK TO VERIFY ON EC2 (no docker on the dev box — do NOT
# build here): a GDAL/PROJ clash. The sfincs-cpu base is Ubuntu 22.04 with its
# own system GDAL/PROJ/GEOS; rasterio + geopandas in the cht closure ALSO bundle
# their own GDAL/PROJ via manylinux wheels. We FORCE the manylinux wheel path
# (--only-binary=:all: for rasterio/pyogrio/shapely/fiona/pyproj) so the geo
# stack uses its OWN bundled GDAL/PROJ and never links the system one — this
# avoids the classic "two GDALs, two PROJ_DATA" segfault. VERIFY on EC2 that the
# build-time smoke (`import rasterio, geopandas; from cht_sfincs import SFINCS`)
# AND a no-input SFINCS exec both succeed in the SAME process; if rasterio picks
# up the system GDAL instead of its wheel, pin PROJ_DATA / GDAL_DATA to the wheel
# paths or drop to a python:slim multi-stage + COPY the sfincs binary.
#
# IMAGE SIZE ESTIMATE (would-be — NO docker on this box; build is a deferred
# EC2/SSM step): the sfincs-cpu base is ~1.0-1.2 GB (Ubuntu 22.04 + Fortran/
# OpenMP runtime + the SFINCS binary). The cht closure venv (numba/llvmlite,
# dask, datashader, netcdf4, rasterio, geopandas, xugrid, scipy — ~128 packages)
# adds ~1.2 GB on disk, pruned of __pycache__/tests/.pyc (~-200 MB). ESTIMATED
# combined image ~2.2-2.6 GB uncompressed. This is heavier than either single
# worker (it carries the binary AND the GPL build closure) and EXCEEDS the 2 GB
# AgentCore Runtime ceiling noted in MEMORY — fine for AWS Batch (no such cap),
# but flag it if the AgentCore Runtime path is ever revisited for this worker.
# Container-hygiene norm: inspect `docker history` + size on EC2 before any ECR
# push (single-stage here because the binary must stay on its native base; the
# prune step below is the size discipline).

FROM deltares/sfincs-cpu:sfincs-v2.3.3@sha256:46b5fc9e324d3d9a0d0a7728390e3413bb1ce57ef74c4201a3003d21ef1efb71

LABEL org.opencontainers.image.title="trid3nt-sfincs-quadtree" \
      org.opencontainers.image.source="https://github.com/double-r-squared/trid3nt-qgis-plugin" \
      org.opencontainers.image.description="COMBINED SFINCS quadtree+SnapWave BUILD+SOLVE worker (AWS Batch, one job). Reads a build-spec JSON, authors a refined quadtree deck via cht_sfincs (GPL-3.0, image-only) with auto-refinement + cell-budget cap + building obstacles, then runs /usr/local/bin/sfincs (MIT) in-process on the local deck and writes sfincs_map.nc + a union completion.json to the object store." \
      org.opencontainers.image.licenses="GPL-3.0-or-later AND MIT"

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# python3 + venv tooling. The sfincs-cpu base is Ubuntu 22.04 (Jammy); pip 22.x
# predates PEP 668 so a venv install is clean. We use an ISOLATED venv (not the
# system python) so the cht closure's GDAL/PROJ wheels never collide with the
# base image's system GDAL/PROJ at the system-site level.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 \
        python3-venv \
        python3-dev \
        git \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --upgrade pip setuptools wheel

# cht_sfincs PINNED to the exact commit proven in the spike venv. Installing
# cht_sfincs pulls its cht_* git deps + the ~120 PyPI deps (numpy/xarray/
# rasterio/geopandas/xugrid/datashader/numba/scipy/netcdf4/...). boto3 +
# google-cloud-storage are the object-store SDKs the entrypoint dispatches on by
# URI scheme.
#
# --prefer-binary keeps the geo stack on manylinux wheels (their OWN bundled
# GDAL/PROJ) so they do NOT link the Ubuntu-22.04 system GDAL/PROJ — the one
# clash to verify on EC2 (see the header note). If a source build is forced for
# rasterio/pyogrio/shapely, the system-GDAL clash becomes possible; the smoke
# below is the tripwire.
ARG CHT_SFINCS_COMMIT=159df40df35042b37c906242bbbe47938652efe9
RUN pip install --no-cache-dir --prefer-binary \
        "cht_sfincs @ git+https://github.com/Deltares-research/cht_sfincs.git@${CHT_SFINCS_COMMIT}" \
        "boto3>=1.34,<2" \
        "google-cloud-storage>=2.18,<4" \
    && python -c "from cht_sfincs import SFINCS; import rasterio, geopandas, xugrid, boto3; print('cht_sfincs + geo stack + boto3 import OK')"

# Prune the venv: byte-compiled caches only. Do NOT delete package `tests/` dirs —
# numpy 2.2.x imports `numpy._core.tests` at runtime (via `numpy.testing`, pulled
# in by scipy's array_api_compat), and other pkgs do similar; deleting them breaks
# `from numpy import *`. The test dirs are small anyway vs the numba/llvmlite/scipy
# binaries that dominate the venv weight, so the hygiene saving isn't worth it.
RUN find /opt/venv -depth -type d -name '__pycache__' -exec rm -rf {} + \
    && find /opt/venv -type f -name '*.pyc' -delete

# Worker code lands on PYTHONPATH so `-m services.workers.sfincs_deckbuilder.entrypoint`
# resolves. Build context is the repo root; paths are repo-root-relative.
RUN mkdir -p /opt/grace2/services/workers/sfincs_deckbuilder
COPY services/workers/sfincs_deckbuilder/ /opt/grace2/services/workers/sfincs_deckbuilder/

# The shared GPL-free NetCDF -> COG postprocess substrate (postprocess-offload
# spike). COPY'd into BOTH SFINCS images (and any future raster worker) so the
# heavy raster postprocess runs ON the worker, not the always-on agent box.
RUN mkdir -p /opt/grace2/services/workers/_raster_postprocess
COPY services/workers/_raster_postprocess/ /opt/grace2/services/workers/_raster_postprocess/

WORKDIR /opt/grace2

ENV PYTHONPATH=/opt/grace2 \
    PYTHONUNBUFFERED=1 \
    TRID3NT_DECK_SCRATCH=/opt/grace2/work \
    TRID3NT_SFINCS_BIN=/usr/local/bin/sfincs \
    GCP_PROJECT=legacy-cloud-project

# Runtime config — overridable from the AWS Batch job-def containerOverrides.
# GCP is decommissioned: the live object store is AWS S3 (TRID3NT_OBJECT_STORE=s3,
# scheme-aware entrypoint). These baked defaults are INERT under AWS Batch (the
# job-def overrides every value per submission) but are pinned to the AWS S3
# names + scheme for honesty.
ENV TRID3NT_OBJECT_STORE=s3 \
    TRID3NT_CACHE_BUCKET=trid3nt-cache \
    TRID3NT_RUNS_BUCKET=trid3nt-runs \
    TRID3NT_RUN_ID="" \
    TRID3NT_BUILD_SPEC_URI=""

# Build-time smoke (the GDAL/PROJ-clash tripwire): the SFINCS binary exists, the
# GPL build closure imports, AND the entrypoint module imports — all in ONE
# process. If the geo stack links the system GDAL/PROJ instead of its wheel, this
# is where it segfaults at build time rather than at Batch exec time.
RUN test -x /usr/local/bin/sfincs \
    && echo "SFINCS binary OK at /usr/local/bin/sfincs" \
    && python -c "from cht_sfincs import SFINCS; import rasterio, geopandas; print('cht_sfincs + geo OK')" \
    && python -c "from services.workers.sfincs_deckbuilder.entrypoint import main, validate_build_spec, compose_manifest, build_deck, _run_sfincs, derive_refinement_polygons, apply_cell_budget, normalize_snapwave_time_columns, emit_quadtree_mesh_geojson, build_mesh_geodataframe, extract_quadtree_faces, run_raster_postprocess; print('combined entrypoint import OK')" \
    && python -c "import xarray; from scipy.interpolate import griddata; import rasterio, pyproj; from pyproj import Transformer; from services.workers._raster_postprocess import postprocess, cog, sfincs_reader, manifest, band_stats; print('raster_postprocess substrate + GDAL/PROJ tripwire OK')"

# One-shot Batch worker: argv flows straight to the entrypoint module; env-var
# fallbacks (TRID3NT_RUN_ID, TRID3NT_BUILD_SPEC_URI) are parsed if no CLI flags.
# ENTRYPOINT overrides the upstream sfincs default so this image runs the
# combined BUILD+SOLVE module, not the bare binary.
ENTRYPOINT ["python", "-m", "services.workers.sfincs_deckbuilder.entrypoint"]
CMD []
