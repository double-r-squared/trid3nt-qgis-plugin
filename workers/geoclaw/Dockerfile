# GeoClaw (Clawpack) solver AWS Batch worker (sprint-17 / FR-CE-1).
#
# Builds the runtime image for the GeoClaw AWS Batch job -- the scale-to-zero
# CLOUD LANE for the GeoClaw shallow-water inundation engine (tsunami / dam-break
# / surge run-up). GeoClaw is the Fortran solver inside Clawpack. The classic
# GeoClaw run workflow is ``make .output``, which COMPILES a per-deck ``xgeoclaw``
# executable from the Clawpack Fortran SOURCE tree (via the
# ``$(CLAW)/clawutil/src/Makefile.common`` machinery) and runs it headless. So
# the image needs a Fortran toolchain (gfortran + make + build-essential) at RUN
# time, AND the full Clawpack SOURCE tree present so ``$(CLAW)/...`` resolves. The
# entrypoint shim performs the S3/GCS-IN -> author-setrun.py -> Clawpack-RUN ->
# S3/GCS-OUT envelope; we do NOT modify the GeoClaw Fortran.
#
# ROOT-CAUSE FIX (why the pip wheel ALONE is not enough): the PyPI ``clawpack``
# distribution installs the Python subpackages (clawpack.geoclaw / clawutil /
# riemann / ...) so ``import clawpack.geoclaw`` works, but it does NOT ship the
# Fortran ``src/`` trees or the Makefile machinery -- there is NO
# ``clawutil/src/Makefile.common`` under site-packages. So a deck authored with
# the canonical ``include $(CLAW)/clawutil/src/Makefile.common`` line (correct for
# a SOURCE install) dies at solve time with
# "Makefile.common: No such file or directory". The fix is to ALSO unpack the
# pinned Clawpack SOURCE tree (Fortran + Makefiles) and point ``$CLAW`` at it. We
# keep the pip install for the Python imports (the deck-author + the generated
# maketopo.py / setrun.py ClawRunData), and use the source tree purely for the
# ``make .output`` Fortran build -- BOTH pinned to the SAME version (5.14.0) so the
# compiled ``xgeoclaw`` and the Python ``ClawRunData`` deck schema agree.
#
# DESIGN NOTE (why NOT multi-stage venv-copy like SWMM): GeoClaw's runtime solve
# is ``make .output``, which COMPILES ``xgeoclaw`` from the Clawpack Fortran
# sources each run. So gfortran + make + the Clawpack SOURCE tree must be PRESENT
# at run time, not just build time. Stripping them (the SWMM venv-copy hygiene)
# would break the solve. We instead keep the single-stage image lean by: a slim
# base, --no-install-recommends, PIP_NO_CACHE_DIR, apt-list cleanup, and a
# checksum-verified source tarball (no .git history) extracted to /opt/clawpack-src
# -- the container-hygiene contract adapted to a compile-at-runtime engine. (Cost
# note: this is the heaviest worker base; the engine is scale-to-zero so it is
# paid only per solve. The first solve in a fresh container pays a one-time
# gfortran compile of xgeoclaw -- see the runtime-compile note below.)
#
# Version pins (mirror the SFINCS/MODFLOW/SWMM digest discipline -- bumps are a
# deliberate, IaC-visible step): python:3.11-slim-bookworm + clawpack 5.14.0
# (both the pip install AND the source tarball, kept lockstep). The source
# tarball is SHA256-pinned below (the digest analogue for a non-image asset).

# --- Stage 1: PETSc + MPI for the Boussinesq (SGN) dispersive variant --------
# The bouss xgeoclaw (num_eqn=5) solves an implicit SGN sparse system every step
# via PETSc + MPI -- absent from the base image, so the num_eqn=5 executable
# cannot link without them. PETSc >=3.20 is MANDATORY: clawpack 5.14's
# petsc_driver.f90 references PetscViewerASCIIStdoutSetFileUnit (added in PETSc
# 3.20); Debian's packaged 3.18 compiles every object but FAILS at link with an
# "undefined reference". conda-forge ships PETSc 3.25 + mpich prebuilt, so this
# stage installs that env and the runtime stage copies ONLY the resolved env
# (the micromamba package-tarball cache under $MAMBA_ROOT_PREFIX never reaches
# the final image -- the multi-stage hygiene win). gfortran+make stay in the env
# because a bouss deck COMPILES per-run (like the shallow path), against the
# conda-built libgfortran/libpetsc ABI, so mpif90's backend compiler must match.
FROM python:3.11-slim-bookworm AS petscbuild

ENV DEBIAN_FRONTEND=noninteractive \
    MAMBA_ROOT_PREFIX=/opt/mamba
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl bzip2 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Pinned micromamba (the digest-discipline analogue for the conda toolchain).
ARG MICROMAMBA_VERSION=2.9.0-0
RUN curl -fsSL \
      "https://github.com/mamba-org/micromamba-releases/releases/download/${MICROMAMBA_VERSION}/micromamba-linux-64" \
      -o /usr/local/bin/micromamba \
    && chmod +x /usr/local/bin/micromamba

RUN micromamba create -y -p /opt/petscenv -c conda-forge \
        "petsc>=3.20" mpich gfortran make pkg-config \
    && micromamba clean -a -y \
    && test -f /opt/petscenv/lib/pkgconfig/petsc.pc \
    && PATH=/opt/petscenv/bin:$PATH mpif90 --version >/dev/null

# Runtime hygiene: keep the libs + compiler (a bouss solve compiles + links +
# runs against them), drop what a solve never reads -- docs, man/info pages, test
# suites, pyc caches. NOTE: static archives (*.a) are DELIBERATELY kept: the
# runtime bouss link pulls libgcc.a / libgfortran.a etc, so purging them breaks
# the compile ("cannot find -lgcc"). Only never-read metadata is removed here.
RUN rm -rf /opt/petscenv/share/doc /opt/petscenv/share/man \
           /opt/petscenv/share/info /opt/petscenv/share/gtk-doc \
           /opt/petscenv/conda-meta \
    && find /opt/petscenv -depth -type d -name '__pycache__' -exec rm -rf {} + \
    && find /opt/petscenv -depth -type d -name 'tests' -path '*site-packages*' -exec rm -rf {} +


FROM python:3.11-slim-bookworm AS runtime

LABEL org.opencontainers.image.title="trid3nt-geoclaw-solver" \
      org.opencontainers.image.source="https://github.com/double-r-squared/trid3nt-qgis-plugin" \
      org.opencontainers.image.description="GeoClaw (Clawpack) shallow-water inundation solver runtime (AWS Batch). Reads a build_spec + topo DEM from s3://...-cache/, authors a setrun.py, runs the headless Clawpack solver, writes fort.q frames + completion manifest to s3://...-runs/<run_id>/." \
      org.opencontainers.image.licenses="BSD-3-Clause"

ENV VENV=/opt/trid3nt/.venv \
    CLAW_SRC=/opt/clawpack-src \
    CLAWPACK_VERSION=5.14.0 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DEBIAN_FRONTEND=noninteractive

# Build + run toolchain. gfortran + make + build-essential are REQUIRED at run
# time (``make .output`` compiles xgeoclaw from the Clawpack Fortran source each
# solve), so they stay in the final image -- see the design note above.
# ca-certificates for boto3 HTTPS; libexpat1 for rasterio's GDAL XML driver (the
# job-0240 lesson the MODFLOW image hit). curl: fetch the pinned Clawpack source
# tarball at build time (BUILD-only, but it is a tiny static binary so we leave it
# rather than add a multi-stage just to drop it).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        gfortran \
        make \
        curl \
        ca-certificates \
        libexpat1 \
    && rm -rf /var/lib/apt/lists/*

# --- Python venv + Clawpack (Python imports only) --------------------------
# The pip install gives the Python subpackages (clawpack.geoclaw / clawutil /
# riemann / ...) used by the deck author + the generated maketopo.py and the
# setrun.py ClawRunData. It does NOT provide the Fortran build machinery (see the
# ROOT-CAUSE FIX note up top) -- that comes from the source tree below. PINNED to
# ==${CLAWPACK_VERSION} so it matches the source tarball exactly (a version skew
# between the Python ClawRunData and the compiled xgeoclaw would corrupt decks).
# clawpack pulls numpy + matplotlib; we add boto3 for the S3 envelope and rasterio
# for the topo<->ASCII handling the entrypoint may do. The Linux/cp311 pip path
# builds clawpack from its sdist (meson + f2py + gfortran), so this stays a long
# layer.
RUN python3 -m venv "$VENV" \
    && "$VENV/bin/pip" install --no-cache-dir --upgrade pip setuptools wheel \
    && "$VENV/bin/pip" install --no-cache-dir \
        "numpy>=1.26,<3" \
        "boto3>=1.34,<2" \
        "rasterio>=1.3,<2" \
        "clawpack==${CLAWPACK_VERSION}" \
    && "$VENV/bin/python" -c "import clawpack, numpy, rasterio, boto3; from clawpack import geoclaw; print('geoclaw deps import OK - clawpack', clawpack.__version__)"

ENV PATH="$VENV/bin:$PATH"

# --- Clawpack SOURCE tree (the Fortran + Makefile machinery) ----------------
# THE root-cause fix: the pip install above has NO clawutil/src/Makefile.common,
# so ``make .output`` cannot resolve $(CLAW)/clawutil/src/Makefile.common. We
# fetch the OFFICIAL release source tarball (clawpack-v${CLAWPACK_VERSION}.tar.gz
# -- the one asset that BUNDLES all submodules, i.e. the full Fortran source +
# Makefiles; the GitHub auto-generated tar/zip do NOT include submodules) and
# unpack it to $CLAW_SRC. SHA256-verified for reproducibility (the digest-pin
# analogue for a non-image asset; bump it in lockstep with CLAWPACK_VERSION).
# Layout at $CLAW_SRC after extraction:
#   $CLAW_SRC/clawutil/src/Makefile.common              (provides the .output rule)
#   $CLAW_SRC/geoclaw/src/2d/shallow/Makefile.geoclaw   (COMMON_MODULES/SOURCES)
#   $CLAW_SRC/{geoclaw,amrclaw,...}/src/...             (the Fortran xgeoclaw builds)
# render_makefile() in setrun_builder.py emits exactly those two include paths
# relative to $(CLAW), so they resolve against this source layout unchanged.
ENV CLAWPACK_SRC_SHA256=ecf9297ca3e483c466fd8ade93192d3c08dec4c30dc1069f501f62c6c4eecb8e
RUN mkdir -p "$CLAW_SRC" \
    && curl -fsSL -o /tmp/clawpack-src.tar.gz \
        "https://github.com/clawpack/clawpack/releases/download/v${CLAWPACK_VERSION}/clawpack-v${CLAWPACK_VERSION}.tar.gz" \
    && echo "${CLAWPACK_SRC_SHA256}  /tmp/clawpack-src.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/clawpack-src.tar.gz -C "$CLAW_SRC" --strip-components=1 \
    && rm -f /tmp/clawpack-src.tar.gz \
    && test -f "$CLAW_SRC/clawutil/src/Makefile.common" \
    && test -f "$CLAW_SRC/geoclaw/src/2d/shallow/Makefile.geoclaw"

# CLAW points at the SOURCE tree (not the pip package) so the per-deck ``make
# .output`` resolves $(CLAW)/clawutil/src/Makefile.common + the GeoClaw Fortran
# sources. The Python imports keep coming from the pip-installed clawpack on
# PYTHONPATH -- CLAW is ONLY the Fortran/Makefile build root.
ENV CLAW=/opt/clawpack-src \
    FC=gfortran

# --- PETSc + MPI env for the Boussinesq (SGN) variant ------------------------
# Copy ONLY the resolved conda env from the builder (no micromamba, no package
# cache). A bouss deck's Makefile (render_makefile, bouss branch) sets
# FC=$(CLAW_MPIFC)=mpif90, includes Makefile.bouss, and links -lpetsc; these
# four vars satisfy Makefile.bouss's hard `ifndef` guards. mpiexec/mpif90 are
# bare names -- the entrypoint prepends /opt/petscenv/bin to PATH ONLY for a
# bouss make subprocess, so the conda `gfortran` in that dir NEVER shadows the
# system gfortran the shallow path compiles with (protecting the unchanged
# shallow lane). MPIR_CVAR_CH4_NETMOD=ofi + FI_PROVIDER=tcp force mpich onto
# libfabric/tcp instead of UCX: UCX's iface-port-speed probe divides by a
# zero link speed in a network-less container, and gfortran's FP-trap turns that
# benign op into a fatal SIGFPE at MPI_Init -- the ofi/tcp path avoids it.
COPY --from=petscbuild /opt/petscenv /opt/petscenv
ENV PETSC_DIR=/opt/petscenv \
    PETSC_ARCH= \
    PETSC_OPTIONS="-options_file /opt/clawpack-src/geoclaw/examples/bouss/petscMPIoptions" \
    CLAW_MPIEXEC=mpiexec \
    CLAW_MPIFC=mpif90 \
    BOUSS_MPI_PROCS=2 \
    OMP_NUM_THREADS=2 \
    MPIR_CVAR_CH4_NETMOD=ofi \
    FI_PROVIDER=tcp

# Build-time PROOF the num_eqn=5 bouss executable compiles + links + provenance
# in THIS image (the ADR 0246 STOP was "cannot compile in-image"). Uses the
# shipped radial_flat example (the board's own smoke row). Fails the BUILD -- not
# a solve -- if PETSc/MPI regress.
#
# CRITICAL cleanup: the bouss compile uses the CONDA gfortran and writes .o/.mod
# into the SHARED $CLAW source tree (amrclaw / geoclaw/shallow) -- the same
# modules the SHALLOW (non-bouss) path compiles with the SYSTEM gfortran. Leaving
# them would make a later shallow solve fail ("regions_module.mod created by a
# different version of GNU Fortran"). So after proving the link we STRIP every
# build artifact from the tree: each ephemeral one-deck container then compiles
# its own path from a clean tree with its own compiler (the original per-run
# model; no baked cross-compiler cache).
RUN set -e; \
    cd "$CLAW/geoclaw/examples/bouss/radial_flat"; \
    PATH=/opt/petscenv/bin:$PATH make new 2>&1 | tail -3; \
    test -x ./xgeoclaw; \
    nm ./xgeoclaw | grep -q '__bouss_module_MOD_' && echo "BOUSS num_eqn=5 SYMBOLS OK"; \
    ldd ./xgeoclaw | grep -q 'libpetsc' && echo "PETSC LINK OK"; \
    grep -q 'clawdata.num_eqn = 5' setrun.py && echo "num_eqn=5 DECK OK"; \
    rm -f ./xgeoclaw ./*.o ./*.mod; \
    find "$CLAW" \( -name '*.o' -o -name '*.mod' \) -delete; \
    echo "source tree cleaned of build artifacts (both lanes compile fresh)"

# COPY the worker package (entrypoint + setrun_builder) onto a stable runtime
# path. Build context is the repo root (mirrors the SFINCS/MODFLOW/SWMM context);
# paths below are relative to repo root. ALSO copy the postprocess + shared
# raster-postprocess packages the entrypoint imports at solve-end
# (``from workers._geoclaw_postprocess import run_geoclaw_postprocess``,
# which itself imports ``workers._raster_postprocess``) -- without
# these the import silently fails inside the entrypoint's non-fatal try/except
# and NO publish_manifest / scratch reap ever runs (mirrors the sfincs/modflow
# Dockerfiles' ``_raster_postprocess`` copy).
RUN mkdir -p /opt/trid3nt/workers/geoclaw
COPY workers/geoclaw/ /opt/trid3nt/workers/geoclaw/
COPY workers/_geoclaw_postprocess/ /opt/trid3nt/workers/_geoclaw_postprocess/
COPY workers/_raster_postprocess/ /opt/trid3nt/workers/_raster_postprocess/

# WORKDIR controls where GeoClaw writes its scratch _output/ before the
# entrypoint uploads it. /opt/trid3nt/work is a writable scratch dir; the
# entrypoint clears + re-creates it per invocation.
WORKDIR /opt/trid3nt

ENV PYTHONPATH=/opt/trid3nt \
    PYTHONUNBUFFERED=1 \
    TRID3NT_GEOCLAW_SCRATCH=/opt/trid3nt/work

# Runtime config — overridable from the AWS Batch container env block.
ENV TRID3NT_OBJECT_STORE=s3 \
    TRID3NT_RUNS_BUCKET="" \
    TRID3NT_RUN_ID="" \
    TRID3NT_MANIFEST_URI="" \
    AWS_REGION=us-west-2

# Build-time smoke: clawpack/geoclaw import + the entrypoint + the (clawpack-free)
# deck-author module import cleanly, AND the deck author can render a setrun.py
# from a synthetic build_spec (no Fortran run), AND the $CLAW source tree holds
# the two Makefile machinery files render_makefile() includes (so a bad source
# fetch fails the build, not a Batch solve). Catches a path/import/fetch mistake
# at build time instead of at Batch exec time.
RUN python3 -c "import clawpack; from clawpack import geoclaw; print('clawpack OK', clawpack.__version__)" \
    && python3 -c "from workers.geoclaw.entrypoint import main; print('entrypoint import OK')" \
    && python3 -c "from workers.geoclaw.setrun_builder import render_setrun_py, GeoClawBuildSpec; \
print('setrun render OK len=', len(render_setrun_py(GeoClawBuildSpec(scenario='dam_break', bbox=(-85.5,29.5,-85.0,30.0), topo_file='topo.asc'))))" \
    && test -f "$CLAW/clawutil/src/Makefile.common" \
    && test -f "$CLAW/geoclaw/src/2d/shallow/Makefile.geoclaw" \
    && echo "CLAW source machinery OK at $CLAW"

# ENTRYPOINT runs this image as a one-shot solver job. CMD is empty so AWS Batch
# containerOverrides command feeds straight through as argv; env-var fallbacks
# (TRID3NT_RUN_ID, TRID3NT_MANIFEST_URI) are parsed by the entrypoint if no CLI
# flags are passed (mirror of the SFINCS/SWMM/MODFLOW pattern).
ENTRYPOINT ["python3", "-m", "workers.geoclaw.entrypoint"]
CMD []
