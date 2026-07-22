# SFINCS solver worker (sprint-07 / M5 / FR-CE-1 / job-0040).
#
# Builds the runtime image for the SFINCS Cloud Run Job. Thin layer over the
# upstream Deltares `deltares/sfincs-cpu` image — we add Python + google-cloud-
# storage + a slim entrypoint shim that performs the GCS-IN -> SFINCS-RUN ->
# GCS-OUT envelope every solver invocation needs. We do NOT modify the SFINCS
# binary or its bundled libraries.
#
# Base-image choice (TENTATIVE — surfaced in report Open Questions):
#   We pull deltares/sfincs-cpu directly from Docker Hub at build time. The
#   alternative is an Artifact Registry pull-through cache (eliminates the
#   Docker Hub anonymous-pull rate limit and gives us a sovereign, audited copy
#   of the upstream digest). Direct pull is fine for sprint-07 v0.1 frequency
#   (~1 build/week max); if rate limits bite, the migration is a one-line
#   Dockerfile change (FROM us-central1-docker.pkg.dev/.../sfincs-cpu@...).
#
# Version pin: `sfincs-v2.3.3` (current upstream stable as of 2026-05-12),
# digest-pinned to keep `tofu plan` an honest source of truth for what's
# deployed. Bump-on-upstream-release is a deliberate, IaC-visible step (mirror
# of the qgis-server.tf + worker.tf digest discipline from job-0018 + job-0021).
#
# Decisions worth surfacing here (also in report.md § Decisions Made):
#   - Thin layer over the official sfincs-cpu image vs hand-built from source:
#     thin layer. Deltares ships the upstream image with the right Fortran
#     toolchain, OpenMP runtime, and SFINCS executable already wired; building
#     from source would shift maintenance load onto us with zero correctness
#     benefit. Trade-off: we depend on Deltares' release cadence.
#   - System pip + --break-system-packages vs venv: system pip. Single-purpose
#     container, no host-Python contamination concern, no PATH dance, matches
#     the job-0021 worker pattern.

FROM deltares/sfincs-cpu:sfincs-v2.3.3@sha256:46b5fc9e324d3d9a0d0a7728390e3413bb1ce57ef74c4201a3003d21ef1efb71

LABEL org.opencontainers.image.title="trid3nt-sfincs-solver" \
      org.opencontainers.image.source="https://github.com/double-r-squared/trid3nt-qgis-plugin" \
      org.opencontainers.image.description="SFINCS solver runtime (Cloud Run Job). Reads model setup from s3://trid3nt-cache/, runs SFINCS, writes outputs + completion manifest to s3://trid3nt-runs/<run_id>/." \
      org.opencontainers.image.licenses="MIT"

# Install Python 3 + google-cloud-storage SDK. The upstream image is
# Ubuntu 22.04 (Jammy) with /usr/local/bin/sfincs (the SFINCS binary, sourced from
# https://github.com/Deltares/SFINCS). 22.04's pip 22.x predates PEP 668, so
# system pip installs work directly — we do NOT need the
# `--break-system-packages` flag the job-0021 worker uses on the
# Debian/qgis-server base (pip 23+).
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip3 install --no-cache-dir \
        "google-cloud-storage>=2.18,<4" \
        "boto3>=1.34,<2" \
        "numpy>=1.24,<3" \
        "scipy>=1.10" \
        "xarray>=2023.1" \
        "rasterio>=1.3,<2" \
        "pyproj>=3.4" \
        "h5netcdf>=1.1" \
        "pyyaml>=6.0" \
        "geopandas>=0.13" \
        "pandas>=2.0,<3" \
        "hydromt-sfincs>=1.1.2,<2.0" \
    && python3 -c "from google.cloud import storage; import boto3; print('storage SDKs import OK (gcs + boto3)')" \
    && python3 -c "import xarray; from scipy.interpolate import griddata; import rasterio, pyproj; from pyproj import Transformer; print('raster_postprocess geo stack + GDAL/PROJ tripwire OK')" \
    && python3 -c "import hydromt_sfincs, geopandas, yaml; print('hydromt-sfincs build stack import OK (heavy-compute offload)')"

# COPY the entrypoint into a stable runtime path. Build context is the repo
# root (see infra/sfincs/cloudbuild.yaml); paths below are relative to repo
# root.
RUN mkdir -p /opt/grace2/services/workers/sfincs
COPY services/workers/sfincs/ /opt/grace2/services/workers/sfincs/

# The shared GPL-free NetCDF -> COG postprocess substrate (postprocess-offload
# spike). COPY'd into BOTH SFINCS images so the regular-grid path does NOT
# regress to raw-NetCDF (the quadtree worker carries the same package).
RUN mkdir -p /opt/grace2/services/workers/_raster_postprocess
COPY services/workers/_raster_postprocess/ /opt/grace2/services/workers/_raster_postprocess/

# The SFINCS regular-grid deck BUILD substrate (heavy-compute offload). Vendored
# pure hydromt-SFINCS build logic (dataclasses + Manning/NLCD gate + adaptive
# autoscale + YAML config generator) + the ``build_sfincs_deck`` orchestrator the
# entrypoint's --build-spec-uri mode calls. The hydromt-sfincs dep installed
# above powers it. Kept in sync with the agent's sfincs_builder.py (see the
# module header); ``services/workers/`` is not on the agent import path.
RUN mkdir -p /opt/grace2/services/workers/_sfincs_build
COPY services/workers/_sfincs_build/ /opt/grace2/services/workers/_sfincs_build/

# WORKDIR controls where SFINCS writes its scratch outputs before the
# entrypoint uploads them. /opt/grace2/work is a writable scratch dir; the
# entrypoint clears it on start and re-creates it for each invocation.
WORKDIR /opt/grace2

ENV PYTHONPATH=/opt/grace2 \
    PYTHONUNBUFFERED=1 \
    TRID3NT_SFINCS_BIN=/usr/local/bin/sfincs \
    TRID3NT_SFINCS_SCRATCH=/opt/grace2/work

# Runtime config — overridable from the AWS Batch job-def containerOverrides.
# GCP is decommissioned: the live object store is AWS S3 (TRID3NT_OBJECT_STORE=s3,
# scheme-aware entrypoint). These baked defaults are INERT under AWS Batch (the
# job-def overrides every value per submission) but are pinned to the AWS S3
# names + scheme for honesty.
#   TRID3NT_OBJECT_STORE  — runs-bucket output scheme (s3 on AWS).
#   TRID3NT_CACHE_BUCKET  — source bucket (the cache shim populates this with
#                          the per-tool fetches the workflow stages).
#   TRID3NT_RUNS_BUCKET   — destination bucket (this image is the only sanctioned
#                          writer of `s3://trid3nt-runs/<run_id>/`).
#   TRID3NT_RUN_ID        — the run ID under which outputs land (set per Batch job).
#   TRID3NT_MANIFEST_URI  — s3:// URI of the JSON setup manifest the entrypoint
#                          reads (lists input objects + bbox + scenario).
#   TRID3NT_BUILD_SPEC_URI — s3:// URI of the agent-composed BUILD job_spec; when
#                          set the worker runs hydromt build + solve + postprocess
#                          (heavy-compute offload). Takes precedence over MANIFEST.
ENV TRID3NT_OBJECT_STORE=s3 \
    TRID3NT_CACHE_BUCKET=trid3nt-cache \
    TRID3NT_RUNS_BUCKET=trid3nt-runs \
    TRID3NT_RUN_ID="" \
    TRID3NT_MANIFEST_URI="" \
    TRID3NT_BUILD_SPEC_URI=""

# Build-time smoke: the SFINCS binary exists where we expect, and the
# entrypoint module imports cleanly. Catches a base-image path shift or a
# Python import mistake at build time instead of at Cloud Run Job exec time.
RUN test -x /usr/local/bin/sfincs \
    && echo "SFINCS binary OK at /usr/local/bin/sfincs" \
    && python3 -c "from services.workers.sfincs.entrypoint import main, run_raster_postprocess; print('entrypoint import OK')" \
    && python3 -c "from services.workers._raster_postprocess import postprocess, cog, sfincs_reader, manifest, band_stats; print('raster_postprocess substrate import OK')" \
    && python3 -c "from services.workers._sfincs_build import build_sfincs_deck, validate_job_spec; print('sfincs_build (offload) substrate import OK')"

# ENTRYPOINT overrides the upstream sfincs default so this image runs as a
# one-shot solver Job, not an interactive container. CMD is empty so Cloud
# Run Job task args feed straight through as argv to the module. Env-var
# fallbacks (TRID3NT_RUN_ID, TRID3NT_MANIFEST_URI) are parsed by the
# entrypoint if no CLI flags are passed (mirror of the worker pattern).
ENTRYPOINT ["python3", "-m", "services.workers.sfincs.entrypoint"]
CMD []
