# MODFLOW 6 solver worker (sprint-13 / MOD-1 / FR-CE-1 / job-0220).
#
# Builds the runtime image for the MODFLOW 6 solver. Unlike the SFINCS worker (a
# thin layer over the upstream Deltares image), MODFLOW 6 has no maintained
# official Docker image matching the USGS release train, so we install the
# version-pinned USGS binaries explicitly with SHA-256 verification.
#
# Two-stage build (ADR 0258 container hygiene): a `binaries` stage carries the
# download/verify/extract toolchain (curl + unzip) and fetches the SHA-pinned
# USGS mf6 6.7.0 + gridgen 1.0.02 binaries; the runtime stage COPYs only the
# resolved binaries in, so curl/unzip never ship in the runtime image. The
# heavy layer (the flopy/rasterio/numpy venv) dominates size either way, but the
# split keeps the download toolchain out of the shipped image.
#
# Base-image choice: python:3.11-slim (Debian Bookworm slim). Python is a
# first-class runtime requirement here -- the deck builder + postprocess use
# FloPy/rasterio, and flopy's Gridgen wrapper shells out to the `gridgen` binary
# at deck-build time. Bookworm's pip 23+ enforces PEP 668, so Python deps install
# into a venv at /opt/trid3nt/.venv rather than --break-system-packages.
#
# MODFLOW binary version pin: MODFLOW 6.7.0 -- the combined GWF + GWT + PRT binary
# `mf6` from the USGS GitHub release zip. The release asset is `mf6.7.0_linux.zip`
# and the canonical MODFLOW-USGS/modflow6 repo path 301-redirects to
# MODFLOW-ORG/modflow6. We pin the SHA-256 of the zip and verify it before
# extraction; -fL follows the canonical->ORG redirect.
#
# gridgen version pin (ADR 0258): USGS gridgen 1.0.02 (public domain, MODFLOW-ORG
# /gridgen release v1.0.02, linux.zip). flopy.utils.gridgen.Gridgen shells out to
# this executable at DISV/DISU build time to refine a base structured grid into a
# vertex (DISV) grid around drawn polygons + wells -- the quad-refinement the PRT
# backward capture-zone rows need. Absent until this ADR (ADR 0246 sweep-2 STOP:
# `which gridgen` = none in-image blocked every DISV/DISU path). triangle (the
# unstructured-Voronoi companion) is NOT installed: the quad-refined DISV path
# uses gridgen polygon refinement only, no Delaunay triangulation.

# --- Stage 1: binary acquisition (download toolchain, discarded) ----------
FROM python:3.11-slim AS binaries

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        unzip \
    && rm -rf /var/lib/apt/lists/*

# mf6 6.7.0 (ADR 0215): the PRT capture-zone / wellhead_protection workflow REQUIRES
# 6.7.0 -- the PRP EXTEND_TRACKING + COORDINATE_CHECK_METHOD options (and the
# transient per-period reversed-budget FMI) do NOT exist in 6.5.0. 6.7.0 is also the
# binary the local-exec supervisor (run_modflow.py) already runs, so the image
# matches the production binary the deck builder is tested against. SHA-256 verified
# from the GitHub release asset digest before the pin.
ARG MF6_ZIP_URL=https://github.com/MODFLOW-ORG/modflow6/releases/download/6.7.0/mf6.7.0_linux.zip
ARG MF6_ZIP_SHA256=f83e743bac1b3009836a44e6e4aa4b4c522ac2e2c7d291e280db47f68353ebdc
# USGS naming is inconsistent: the release zip + its top-level dir are both
# `mf6.7.0_linux` (the leading "6." is dropped), even though `mf6 --version`
# reports "6.7.0".
ARG MF6_ZIP_DIR=mf6.7.0_linux
RUN curl -fL "$MF6_ZIP_URL" -o /tmp/mf6.zip \
    && echo "${MF6_ZIP_SHA256}  /tmp/mf6.zip" | sha256sum -c - \
    && unzip -o /tmp/mf6.zip "${MF6_ZIP_DIR}/bin/mf6" "${MF6_ZIP_DIR}/bin/libmf6.so" -d /tmp/mf6_extracted \
    && install -D -m 755 "/tmp/mf6_extracted/${MF6_ZIP_DIR}/bin/mf6" /out/mf6 \
    && install -D -m 755 "/tmp/mf6_extracted/${MF6_ZIP_DIR}/bin/libmf6.so" /out/libmf6.so

# gridgen 1.0.02 (ADR 0258): dedicated MODFLOW-ORG/gridgen release, single-binary
# linux.zip. SHA-256 verified before extraction. The binary is dynamically linked
# against libstdc++.so.6 + libgcc_s.so.1 (added to the runtime apt layer below).
ARG GRIDGEN_ZIP_URL=https://github.com/MODFLOW-ORG/gridgen/releases/download/v1.0.02/linux.zip
ARG GRIDGEN_ZIP_SHA256=d45bc3378f6bc5767f5a5ab7f102c5cc4624d24bad772422762e33d9aca09302
RUN curl -fL "$GRIDGEN_ZIP_URL" -o /tmp/gridgen.zip \
    && echo "${GRIDGEN_ZIP_SHA256}  /tmp/gridgen.zip" | sha256sum -c - \
    && unzip -o /tmp/gridgen.zip gridgen -d /tmp/gridgen_extracted \
    && install -D -m 755 /tmp/gridgen_extracted/gridgen /out/gridgen

# --- Stage 2: runtime -----------------------------------------------------
FROM python:3.11-slim

LABEL org.opencontainers.image.title="trid3nt-modflow-solver" \
      org.opencontainers.image.source="https://github.com/double-r-squared/trid3nt-qgis-plugin" \
      org.opencontainers.image.description="MODFLOW 6 (mf6 6.7.0) + gridgen 1.0.02 solver runtime. Builds a FloPy GWF/GWT/PRT deck (structured or gridgen-refined DISV), runs mf6, writes heads/concentration/pathline outputs + completion manifest." \
      org.opencontainers.image.licenses="MIT"

# --- Runtime system deps (single layer) ----------------------------------
# ca-certificates (HTTPS for pip + boto3), libexpat1 (rasterio's manylinux wheel
# dlopen()s libexpat.so.1 -- GDAL's XML/GML chain -- but does not bundle it),
# libstdc++6 + libgcc-s1 (the gridgen binary links them dynamically). No curl/unzip:
# the binaries arrive pre-fetched from the `binaries` stage.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ca-certificates \
        libexpat1 \
        libstdc++6 \
        libgcc-s1 \
    && rm -rf /var/lib/apt/lists/*

# --- Python venv + deps (single layer) -----------------------------------
# venv at /opt/trid3nt/.venv to sidestep PEP 668 on Bookworm. flopy backs the
# deck builder + postprocess (and the gridgen wrapper); the entrypoint object-store
# envelope is scheme-aware (boto3 drives s3:// / MinIO); numpy/rasterio back the
# postprocess COG conversion; pyproj backs the CRS reprojection. geopandas +
# shapely are REQUIRED by flopy 3.10's flopy.utils.gridgen.Gridgen.build() -- it
# constructs the refined-grid intersection through geopandas geometry ops, so a
# gridgen-refined DISV deck cannot build without them (ADR 0258; matches the
# server venvs/agent which already ships geopandas 1.x + shapely 2.x).
ENV VENV=/opt/trid3nt/.venv
RUN python3 -m venv "$VENV" \
    && "$VENV/bin/pip" install --no-cache-dir --upgrade pip \
    && "$VENV/bin/pip" install --no-cache-dir \
        "flopy>=3.7,<4" \
        "google-cloud-storage>=2.18,<4" \
        "boto3>=1.34,<2" \
        "numpy>=1.26,<3" \
        "rasterio>=1.3,<2" \
        "pyproj>=3.6,<4" \
        "geopandas>=1,<2" \
        "shapely>=2,<3" \
        "pyshp>=2.3,<3" \
    && "$VENV/bin/python" -c "import flopy, numpy, rasterio, boto3, pyproj, geopandas, shapely, shapefile; from google.cloud import storage; print('python deps import OK -- flopy', flopy.__version__, 'geopandas', geopandas.__version__, 'pyshp', shapefile.__version__)"

# venv on PATH so `python3`/`pip` resolve to the venv for all later layers.
ENV PATH="$VENV/bin:$PATH"

# --- COPY the SHA-pinned USGS binaries from the builder stage -------------
COPY --from=binaries /out/mf6 /usr/local/bin/mf6
COPY --from=binaries /out/libmf6.so /usr/local/lib/libmf6.so
COPY --from=binaries /out/gridgen /usr/local/bin/gridgen

# --- COPY entrypoint + fixtures ------------------------------------------
# Build context is the repo root; paths below are relative to repo root.
RUN mkdir -p /opt/trid3nt/services/workers/modflow
COPY services/workers/modflow/ /opt/trid3nt/services/workers/modflow/

# --- COPY the shared offload substrate packages --------------------------
# The --build-spec-uri path imports the agent-import-free job_spec contract
# (_modflow_build), the plume postprocess (_modflow_postprocess), and the shared
# raster manifest/upload helpers (_raster_postprocess). All are PEP-420 namespace
# subpackages under services/workers/ (no __init__ at services/ level).
RUN mkdir -p /opt/trid3nt/services/workers/_raster_postprocess \
    && mkdir -p /opt/trid3nt/services/workers/_modflow_build \
    && mkdir -p /opt/trid3nt/services/workers/_modflow_postprocess
COPY services/workers/_raster_postprocess/ /opt/trid3nt/services/workers/_raster_postprocess/
COPY services/workers/_modflow_build/ /opt/trid3nt/services/workers/_modflow_build/
COPY services/workers/_modflow_postprocess/ /opt/trid3nt/services/workers/_modflow_postprocess/

# WORKDIR controls where mf6 writes its scratch outputs before the entrypoint
# uploads them. /opt/trid3nt/work is a writable scratch dir; the entrypoint
# clears it on start and re-creates it per invocation.
WORKDIR /opt/trid3nt

ENV PYTHONPATH=/opt/trid3nt \
    PYTHONUNBUFFERED=1 \
    TRID3NT_MF6_BIN=/usr/local/bin/mf6 \
    TRID3NT_GRIDGEN_BIN=/usr/local/bin/gridgen \
    TRID3NT_MF6_SCRATCH=/opt/trid3nt/work

# MODFLOW runs the mf6 binary directly via the local-exec supervisor (run_modflow.py)
# on the box, or through this image on the local-docker seam. The bucket defaults
# below are inert on the live local-exec path.
ENV TRID3NT_CACHE_BUCKET=trid3nt-cache \
    TRID3NT_RUNS_BUCKET=trid3nt-runs \
    TRID3NT_RUN_ID="" \
    TRID3NT_MANIFEST_URI=""

# Build-time smoke: the mf6 + gridgen binaries exist + run (provenance), and the
# entrypoint + offload substrate + adapter import cleanly. Catches a path shift or
# import mistake at build time instead of at exec.
RUN test -x /usr/local/bin/mf6 \
    && mf6 --version \
    && echo "mf6 binary OK at /usr/local/bin/mf6" \
    && test -x /usr/local/bin/gridgen \
    && gridgen 2>&1 | grep -q "GRIDGEN Version 1.0.02" \
    && echo "gridgen 1.0.02 binary OK at /usr/local/bin/gridgen" \
    && python3 -c "from services.workers.modflow.entrypoint import main; print('entrypoint import OK')" \
    && python3 -c "from services.workers._modflow_build import validate_job_spec, build_deck_kwargs_from_spec; print('modflow_build (offload) substrate import OK')" \
    && python3 -c "from services.workers._modflow_postprocess import run_plume_postprocess; from services.workers._raster_postprocess import manifest, upload; print('modflow_postprocess substrate import OK')" \
    && python3 -c "from services.workers.modflow import gwt_adapter; print('gwt_adapter (build) import OK -- has build_modflow_deck:', hasattr(gwt_adapter, 'build_modflow_deck'), '-- gridgen_available:', gwt_adapter.gridgen_available())"

# ENTRYPOINT runs this image as a one-shot solver Job. CMD is empty so task args
# feed straight through as argv; env-var fallbacks (TRID3NT_RUN_ID,
# TRID3NT_MANIFEST_URI) are parsed by the entrypoint if no CLI flags are passed.
ENTRYPOINT ["python3", "-m", "services.workers.modflow.entrypoint"]
CMD []
