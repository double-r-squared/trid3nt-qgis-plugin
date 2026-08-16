# HEC-RAS 6.x Linux computation-engine worker (mesh wave M3 / hecras_geometry).
#
# Runtime image carrying HEC's official public-domain 6.6 Linux compute engines
# (RasGeomPreprocess / RasUnsteady / RasSteady) so the geometry preprocess +
# unsteady solve run headless, no Windows, no GUI. The M3 acceptance gate is
# MUNCIE REPLICATION: HEC's own shipped Muncie test project (White River,
# Muncie IN) preprocessed + solved on this Linux stack, hydraulic property
# tables verified bit-identical to the GUI-computed baseline.
#
# This is the mesh-layer wave, NOT the full HEC-RAS engine landing -- there is
# no registered tool / template / contract archetype yet. The image proves the
# geometry pipeline (preprocess -> property tables -> unsteady results) end to
# end on a US-shipped model.
#
# ENGINE PROVENANCE (public domain, freely redistributable): HEC's terms --
# "The HEC-RAS executable code and documentation was developed with U.S. Federal
# Government resources and is therefore in the public domain. It may be used,
# copied, distributed, or redistributed freely." (feasibility report S6). The
# binaries are baked from HEC's OFFICIAL Linux_RAS_v66.zip, SHA-256 pinned +
# verified BEFORE extraction (mirror of the modflow worker's mf6 install). We do
# NOT modify the binaries. HEC acknowledgment carried in the image label below.
#
# Binary ABI: the 6.6 engines were compiled for x64 under RHEL 8 (glibc 2.28)
# with Intel oneAPI Fortran 2021.4.0 + MKL. python:3.11-slim is Debian Bookworm
# (glibc 2.36) -- forward-compatible with the glibc-2.28 target (verified: every
# dependent .so resolves against the bundled Intel/MKL/rhel_8 libs + system
# glibc, and the full Muncie preprocess+unsteady runs green). The zip bundles
# libgfortran.so.5 / libquadmath.so.0 (rhel_8/) + the Intel runtime + MKL, so no
# Fortran-runtime apt package is needed; LD_LIBRARY_PATH points at the bundle.
#
# Base-image choice mirrors the modflow worker (python:3.11-slim): Python is a
# first-class runtime requirement here -- ras-commander (MIT) drives the 6.x
# engines + reads results/geometry HDF via pure h5py, and the entrypoint's
# property-table verification uses h5py/numpy.
#
# CONTAINER HYGIENE (hard rule -- size breakdown reported at build time in
# ADR 0100): multi-stage keeps the 218 MB zip + curl/unzip out of the runtime
# layer; .dockerignore trims tests/pyc from the build context; the bundled MKL
# (~570 MB) is the dominant, irreducible cost of a refinement-grade solver (MKL
# dispatches the CPU-appropriate kernel at run time, so trimming variants needs
# per-CPU runtime validation -- characterized, not done in M3).

# ---------------------------------------------------------------------------
# Stage 1 -- fetch: download HEC's official Linux_RAS_v66.zip, SHA-256 verify
# BEFORE extraction, unpack the engines + bundled libs + the Muncie test case.
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS fetch

ARG RAS_ZIP_URL=https://www.hec.usace.army.mil/software/hec-ras/downloads/Linux_RAS_v66.zip
# SHA-256 of Linux_RAS_v66.zip (HEC, dated 2024-10-15), verified on the host
# Muncie replication run 2026-08-03 (ADR 0100 evidence).
ARG RAS_ZIP_SHA256=e77271a473da5da28b5a95ebf019f77ba3d32fb6341ad43be3ad4a6004c60e4a

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ca-certificates curl unzip \
    && rm -rf /var/lib/apt/lists/*

# Pinned-SHA verify BEFORE extraction (design discipline: mirror of the mf6
# install). The zip's execute bits are not preserved through unzip, so the
# runtime stage re-applies chmod +x on the engines.
RUN curl -fL "$RAS_ZIP_URL" -o /tmp/ras.zip \
    && echo "${RAS_ZIP_SHA256}  /tmp/ras.zip" | sha256sum -c - \
    && unzip -q /tmp/ras.zip -d /tmp/ras_extracted \
    && rm /tmp/ras.zip \
    && test -f /tmp/ras_extracted/Linux_RAS_v66/bin/RasGeomPreprocess

# ---------------------------------------------------------------------------
# Stage 2 -- runtime: python:3.11-slim + the engines/libs + ras-commander.
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS runtime

LABEL org.opencontainers.image.title="trid3nt-hecras-worker" \
      org.opencontainers.image.description="HEC-RAS 6.6 Linux computation engines (RasGeomPreprocess/RasUnsteady/RasSteady) headless worker. Mesh wave M3: geometry preprocess -> hydraulic property tables -> unsteady results, gated on Muncie (White River, IN) replication. HEC-RAS is public-domain U.S. Federal Government software (acknowledgment: U.S. Army Corps of Engineers, Hydrologic Engineering Center)." \
      org.opencontainers.image.licenses="MIT AND (Public-Domain-US-Gov)"

# libgomp1: the Intel OpenMP runtime (libiomp5) is bundled, but keep the GNU
# OpenMP runtime present as a safety net for any GOMP symbol the engines touch.
# libexpat1: ras-commander pulls rasterio, whose manylinux wheel dlopen()s
# libexpat.so.1 at import (GDAL's XML/GML driver chain) but does not bundle it --
# the modflow-worker lesson; without it `import ras_commander` -> rasterio fails.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        libgomp1 \
        libexpat1 \
    && rm -rf /var/lib/apt/lists/*

# --- HEC-RAS engines + bundled libs (from the fetch stage) -------------------
# /opt/hecras/{bin,libs} mirrors HEC's zip layout. The engines lost their exec
# bit through unzip, so COPY --chmod=755 restores it AT COPY TIME -- a separate
# `chmod +x` RUN would rewrite the whole 170 MB bin/ tree into a second layer
# (container-hygiene: no duplicate binary layer). libs are .so's (loaded, not
# executed) so they keep default perms.
COPY --from=fetch --chmod=755 /tmp/ras_extracted/Linux_RAS_v66/bin/  /opt/hecras/bin/
COPY --from=fetch /tmp/ras_extracted/Linux_RAS_v66/libs/ /opt/hecras/libs/

# --- Python layer: ras-commander (MIT) drives the 6.x engines + reads the
# geometry/results HDF via pure h5py (feasibility report S1/S2). h5py + numpy
# back the entrypoint's property-table verification. ras-commander pulls a heavy
# geo closure (scipy/pandas/geopandas/rasterio/xarray ~700 MB) -- only its Hdf*
# + RasCmdr legs are used on Linux; the trim path (--no-deps + h5py/numpy/pandas/
# xarray) is characterized in ADR 0100 for the engine-landing wave.
ENV VENV=/opt/trid3nt/.venv
RUN python3 -m venv "$VENV" \
    && "$VENV/bin/pip" install --no-cache-dir --upgrade pip \
    && "$VENV/bin/pip" install --no-cache-dir \
        "ras-commander>=0.99,<1" \
        "h5py>=3.10,<4" \
        "numpy>=1.26,<3" \
    && "$VENV/bin/python" -c "import h5py, numpy; from ras_commander import HdfResultsMesh, HdfBase; print('python deps import OK -- h5py', h5py.__version__)"
ENV PATH="$VENV/bin:$PATH"

# --- worker code + Muncie fixture --------------------------------------------
# Build context is THIS worker dir (mirror of the telemac worker); .dockerignore
# trims tests/pyc. The fixture (Muncie wrk_source, ~4.3 MB) is baked so the
# in-container Muncie gate is self-contained.
COPY . /opt/trid3nt/workers/hecras/

# LD_LIBRARY_PATH points at the bundled Intel/MKL/rhel_8 libs (HEC's run scripts
# set exactly this order: libs : libs/mkl : libs/rhel_8). PATH carries the
# engines. TRID3NT_HECRAS_* are the entrypoint's contract.
ENV LD_LIBRARY_PATH="/opt/hecras/libs:/opt/hecras/libs/mkl:/opt/hecras/libs/rhel_8" \
    PATH="/opt/hecras/bin:${PATH}" \
    PYTHONPATH=/opt/trid3nt \
    PYTHONUNBUFFERED=1 \
    TRID3NT_HECRAS_BIN_DIR=/opt/hecras/bin \
    TRID3NT_HECRAS_DATA_DIR=/data

WORKDIR /data

# Build-time smoke: engines exist + are executable, dynamically link cleanly
# against the bundled libs (no "not found"), and the entrypoint imports.
RUN test -x /opt/hecras/bin/RasGeomPreprocess \
    && test -x /opt/hecras/bin/RasUnsteady \
    && ! ( ldd /opt/hecras/bin/RasGeomPreprocess 2>&1 | grep -i "not found" ) \
    && echo "HEC-RAS engines link OK against bundled libs" \
    && python3 -c "import sys; sys.path.insert(0,'/opt/trid3nt/workers/hecras'); import entrypoint; print('hecras entrypoint import OK')"

# One-shot worker: the agent bind-mounts a rundir at /data carrying the deck +
# manifest.json; the entrypoint runs geompre (if requested) -> unsteady ->
# extracts volume accounting + max WSE -> writes hecras_metrics.json into /data.
ENTRYPOINT ["python3", "/opt/trid3nt/workers/hecras/entrypoint.py"]
CMD []
