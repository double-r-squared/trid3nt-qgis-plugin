# SCHISM cross-scale hydrodynamic solver worker (SCHISM spike / ADR 0115).
#
# Builds SCHISM from source (there is no maintained official image tracking the
# release train -- the modflow-worker posture) at the pinned v5.11.0 tag, with
# gfortran + OpenMPI + netCDF-Fortran. TWO executables are baked:
#
#   pschism_WWM_COSINE_ICM_FIB_SED_ANALYSIS_PREC_EVAP_PAHM_HA_MARSH_GEN_AGE_TVD-VL
#     -- the "full-monty" single executable: the semi-implicit hydro core plus
#     ALL compile-time modules that coexist in one binary (waves WWM-III,
#     sediment SED3D, water quality ICM + CoSiNE + FIB, generic + age tracers,
#     parametric hurricane PaHM, marsh, harmonic analysis, precip/evap). Proves
#     the full-engine-control build. NOTE (ADR 0115): a full-monty binary
#     UNCONDITIONALLY initializes every compiled tracer module, so every run
#     must supply that module's namelist (icm.nml, sediment.nml, ...) -- the
#     practical pattern is targeted variants, hence the second binary.
#
#   pschism_TVD-VL
#     -- the hydro-core executable: the clean barotropic/baroclinic solver with
#     no modules, the STOFS-class default for a plain surge/tide run and the
#     binary the in-image QuarterAnnulus verification gate exercises.
#
# TWO documented findings baked as source patches / exclusions (ADR 0115):
#   * EcoSim (USE_ECO) is EXCLUDED -- its automatic-array declarations are
#     illegal under the -finit-local-zero safety flag SCHISM's hydro core wants
#     (gfortran: "Automatic array cannot have an initializer"); it needs ifort
#     or a per-module flag exception, out of scope for one gfortran binary.
#   * MARSH (USE_MARSH) carries two one-line upstream fixes: v5.11.0's
#     schism_init.F90:6463 and schism_step.F90:9660 write `if(iof_marsh(1)==1)`
#     with no `then`, which gfortran rejects. Patched below (documented).
#
# CONTAINER HYGIENE (hard rule, size reported in ADR 0115): multi-stage -- the
# Fortran/C toolchain + *-dev headers + the cloned source tree (~700 MB) stay in
# the build stage and never reach runtime; runtime carries only the two ~7 MB
# executables + the netCDF/MPI/HDF5 runtime .so's + a slim python postprocess
# venv (numpy/xarray/netCDF4 -- SCHISM outputs are netCDF).

# ---------------------------------------------------------------------------
# Stage 1 -- build: compile SCHISM v5.11.0 (pinned commit) with all modules.
# ---------------------------------------------------------------------------
FROM python:3.11-slim-bookworm AS build

# Pin to the v5.11.0 release commit (cryptographic pin -- a moving tag is not
# reproducible). Verified BEFORE building.
ARG SCHISM_TAG=v5.11.0
ARG SCHISM_COMMIT=4d350e49481c625002ee2bf7d7fca32777f53c65

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ca-certificates git make cmake perl python-is-python3 \
        gfortran gcc g++ \
        libopenmpi-dev openmpi-bin \
        libnetcdf-dev libnetcdff-dev libhdf5-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
RUN git clone --depth 1 --branch "${SCHISM_TAG}" https://github.com/schism-dev/schism.git schism \
    && cd schism \
    && test "$(git rev-parse HEAD)" = "${SCHISM_COMMIT}" \
    && echo "SCHISM pinned commit verified: ${SCHISM_COMMIT}"

# --- MARSH upstream fix (two missing `then`; documented ADR 0115) ------------
RUN cd /src/schism \
    && sed -i -E '6463s/if\(iof_marsh\(1\)==1\)\s*$/if(iof_marsh(1)==1) then/' src/Hydro/schism_init.F90 \
    && sed -i -E '9660s/if\(iof_marsh\(1\)==1\)\s*$/if(iof_marsh(1)==1) then/' src/Hydro/schism_step.F90 \
    && grep -q 'if(iof_marsh(1)==1) then' src/Hydro/schism_init.F90 \
    && grep -q 'if(iof_marsh(1)==1) then' src/Hydro/schism_step.F90

# --- cmake cache-init: compilers + flags shared by both builds ---------------
# -finit-local-zero is the SCHISM gfortran safety default (uninitialized locals
# -> 0); -fallow-argument-mismatch is required for the mpi/legacy interfaces
# under gfortran >= 10. netCDF + MPI are auto-discovered (nf-config on PATH,
# find_package(MPI)); ParMETIS is bundled in the source tree.
RUN cat > /src/COMPILERS.cmake <<'EOF'
set(CMAKE_Fortran_COMPILER gfortran CACHE PATH "")
set(CMAKE_C_COMPILER gcc CACHE PATH "")
set(CMAKE_Fortran_FLAGS_RELEASE "-O2 -ffree-line-length-none -static-libgfortran -finit-local-zero -fallow-argument-mismatch" CACHE STRING "" FORCE)
set(CMAKE_BUILD_TYPE Release CACHE STRING "" FORCE)
set(NO_PARMETIS OFF CACHE BOOLEAN "" FORCE)
set(TVD_LIM VL CACHE STRING "" FORCE)
set(OLDIO OFF CACHE BOOLEAN "" FORCE)
EOF

# full-monty module toggles (EcoSim intentionally excluded -- see header)
RUN cat > /src/FULLMONTY.cmake <<'EOF'
set(USE_WWM ON CACHE BOOLEAN "" FORCE)
set(USE_SED ON CACHE BOOLEAN "" FORCE)
set(USE_ICM ON CACHE BOOLEAN "" FORCE)
set(USE_GEN ON CACHE BOOLEAN "" FORCE)
set(USE_AGE ON CACHE BOOLEAN "" FORCE)
set(USE_COSINE ON CACHE BOOLEAN "" FORCE)
set(USE_FIB ON CACHE BOOLEAN "" FORCE)
set(USE_MARSH ON CACHE BOOLEAN "" FORCE)
set(USE_PAHM ON CACHE BOOLEAN "" FORCE)
set(USE_HA ON CACHE BOOLEAN "" FORCE)
set(USE_ANALYSIS ON CACHE BOOLEAN "" FORCE)
set(PREC_EVAP ON CACHE BOOLEAN "" FORCE)
EOF

# Build full-monty then hydro-core (separate out-of-source trees).
RUN mkdir -p /src/schism/build_full \
    && cd /src/schism/build_full \
    && cmake -C /src/COMPILERS.cmake -C /src/FULLMONTY.cmake /src/schism/src > cfg.log 2>&1 \
    && make -j"$(nproc)" pschism 2>&1 | tail -3 \
    && ls bin/pschism_WWM_*

RUN mkdir -p /src/schism/build_hydro \
    && cd /src/schism/build_hydro \
    && cmake -C /src/COMPILERS.cmake /src/schism/src > cfg.log 2>&1 \
    && make -j"$(nproc)" pschism 2>&1 | tail -3 \
    && ls bin/pschism_TVD-VL

RUN mkdir -p /opt/schism/bin \
    && cp /src/schism/build_full/bin/pschism_* /opt/schism/bin/ \
    && cp /src/schism/build_hydro/bin/pschism_TVD-VL /opt/schism/bin/ \
    && strip /opt/schism/bin/pschism_* \
    && ls -la /opt/schism/bin/

# ---------------------------------------------------------------------------
# Stage 2 -- runtime: slim python:3.11-slim + the executables + runtime libs.
# ---------------------------------------------------------------------------
FROM python:3.11-slim-bookworm AS runtime

LABEL org.opencontainers.image.title="trid3nt-schism-worker" \
      org.opencontainers.image.description="SCHISM v5.11.0 semi-implicit cross-scale hydrodynamic solver (full-monty WWM/SED/ICM/CoSiNE/FIB/GEN/AGE/PaHM/marsh + hydro-core). Barotropic verification gated on Test_QuarterAnnulus (Lynch-Gray analytical M2 tidal)." \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.source="https://github.com/schism-dev/schism"

# Runtime shared libs: netCDF-Fortran (+ its netCDF-C/HDF5 deps), the OpenMPI
# runtime + mpirun (openmpi-bin), and libgomp (OpenMP). apt pulls the transitive
# libnetcdf/libhdf5/libopenmpi runtime .so's the executables dlopen.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        libnetcdff7 openmpi-bin libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Python postprocess venv: SCHISM writes netCDF (scribed out2d_*.nc / per-var
# 3D files); numpy/xarray/netCDF4 read them, numpy also backs the gr3 bridge +
# the QuarterAnnulus gate.
ENV VENV=/opt/trid3nt/.venv
RUN python3 -m venv "$VENV" \
    && "$VENV/bin/pip" install --no-cache-dir --upgrade pip \
    && "$VENV/bin/pip" install --no-cache-dir \
        "numpy>=1.26,<3" "xarray>=2024.1" "netCDF4>=1.6,<2" \
    && "$VENV/bin/python" -c "import numpy, xarray, netCDF4; print('postprocess deps OK')"
ENV PATH="$VENV/bin:$PATH"

COPY --from=build /opt/schism/bin/ /opt/schism/bin/
COPY . /opt/trid3nt/services/workers/schism/

ENV PATH="/opt/schism/bin:${PATH}" \
    PYTHONPATH=/opt/trid3nt/services/workers/schism \
    PYTHONUNBUFFERED=1 \
    SCHISM_BIN_DIR=/opt/schism/bin \
    OMPI_ALLOW_RUN_AS_ROOT=1 \
    OMPI_ALLOW_RUN_AS_ROOT_CONFIRM=1

WORKDIR /data

# Build-time smoke: both executables report a version + link cleanly, the bridge
# imports, and the QuarterAnnulus analytical gate passes with the baked binary.
RUN /opt/schism/bin/pschism_TVD-VL -v \
    && /opt/schism/bin/pschism_WWM_COSINE_ICM_FIB_SED_ANALYSIS_PREC_EVAP_PAHM_HA_MARSH_GEN_AGE_TVD-VL -v \
    && ! ( ldd /opt/schism/bin/pschism_TVD-VL 2>&1 | grep -i "not found" ) \
    && python3 -c "import sys; sys.path.insert(0,'/opt/trid3nt/services/workers/schism'); import schism_gr3; print('gr3 bridge import OK')" \
    && python3 /opt/trid3nt/services/workers/schism/fixtures/quarterannulus/qa_gate.py

ENTRYPOINT ["python3", "/opt/trid3nt/services/workers/schism/entrypoint.py"]
CMD []
