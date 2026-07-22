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

FROM python:3.11-slim-bookworm AS runtime

LABEL org.opencontainers.image.title="grace-2-geoclaw-solver" \
      org.opencontainers.image.source="https://github.com/double-r-squared/GRACE-2" \
      org.opencontainers.image.description="GeoClaw (Clawpack) shallow-water inundation solver runtime (AWS Batch). Reads a build_spec + topo DEM from s3://...-cache/, authors a setrun.py, runs the headless Clawpack solver, writes fort.q frames + completion manifest to s3://...-runs/<run_id>/." \
      org.opencontainers.image.licenses="BSD-3-Clause"

ENV VENV=/opt/grace2/.venv \
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

# COPY the worker package (entrypoint + setrun_builder) onto a stable runtime
# path. Build context is the repo root (mirrors the SFINCS/MODFLOW/SWMM context);
# paths below are relative to repo root.
RUN mkdir -p /opt/grace2/services/workers/geoclaw
COPY services/workers/geoclaw/ /opt/grace2/services/workers/geoclaw/

# WORKDIR controls where GeoClaw writes its scratch _output/ before the
# entrypoint uploads it. /opt/grace2/work is a writable scratch dir; the
# entrypoint clears + re-creates it per invocation.
WORKDIR /opt/grace2

ENV PYTHONPATH=/opt/grace2 \
    PYTHONUNBUFFERED=1 \
    GRACE2_GEOCLAW_SCRATCH=/opt/grace2/work

# Runtime config — overridable from the AWS Batch container env block.
ENV GRACE2_OBJECT_STORE=s3 \
    GRACE2_RUNS_BUCKET="" \
    GRACE2_RUN_ID="" \
    GRACE2_MANIFEST_URI="" \
    AWS_REGION=us-west-2

# Build-time smoke: clawpack/geoclaw import + the entrypoint + the (clawpack-free)
# deck-author module import cleanly, AND the deck author can render a setrun.py
# from a synthetic build_spec (no Fortran run), AND the $CLAW source tree holds
# the two Makefile machinery files render_makefile() includes (so a bad source
# fetch fails the build, not a Batch solve). Catches a path/import/fetch mistake
# at build time instead of at Batch exec time.
RUN python3 -c "import clawpack; from clawpack import geoclaw; print('clawpack OK', clawpack.__version__)" \
    && python3 -c "from services.workers.geoclaw.entrypoint import main; print('entrypoint import OK')" \
    && python3 -c "from services.workers.geoclaw.setrun_builder import render_setrun_py, GeoClawBuildSpec; \
print('setrun render OK len=', len(render_setrun_py(GeoClawBuildSpec(scenario='dam_break', bbox=(-85.5,29.5,-85.0,30.0), topo_file='topo.asc'))))" \
    && test -f "$CLAW/clawutil/src/Makefile.common" \
    && test -f "$CLAW/geoclaw/src/2d/shallow/Makefile.geoclaw" \
    && echo "CLAW source machinery OK at $CLAW"

# ENTRYPOINT runs this image as a one-shot solver job. CMD is empty so AWS Batch
# containerOverrides command feeds straight through as argv; env-var fallbacks
# (GRACE2_RUN_ID, GRACE2_MANIFEST_URI) are parsed by the entrypoint if no CLI
# flags are passed (mirror of the SFINCS/SWMM/MODFLOW pattern).
ENTRYPOINT ["python3", "-m", "services.workers.geoclaw.entrypoint"]
CMD []
