# The TELEMAC worker image: one manifest in, one solved run directory out.
#
# The launcher bind-mounts a run directory at /data (``docker run -v
# <rundir>:/data -w /data``) carrying manifest.json and every staged input. The
# entrypoint reads the manifest, runs ONE dispatch, and writes the results plus
# telemac_metrics.json back into the same directory. The container does no
# network and no object-store I/O, so it needs no boto3 and no credentials: the
# supervisor uploads whatever it wrote.
#
# ENGINE + PIN DISCIPLINE:
#   * opentelemac v9.0.0 (conda-forge, GPL-3) ships the precompiled solvers, the
#     telapy API the runner drives, and the SELAFIN tooling; the conda activation
#     sets $HOMETEL/$SYSTELCFG/$USETELCFG.
#   * numpy==1.26.4 is MANDATORY: numpy 2.x breaks TELEMAC's _hermes C ext.
#   * rasterio<1.4 is the numpy-1 / GDAL ABI-safe line (self-contained manylinux
#     wheel; no conda GDAL to conflict).
#   * scipy backs opentelemac's own SELAFIN readers, pyproj the two in-worker
#     builders' projections. Nothing in the worker or the engine reads geometry,
#     so no shapely.
# A version skew here is a deliberate, IaC-visible bump.
#
# Build context = THIS worker dir, not the repo root (a .dockerignore trims
# tests/pyc):
#   docker build -t trid3nt-local/telemac:latest workers/telemac/

FROM mambaorg/micromamba:2.0.5-debian12-slim AS runtime

LABEL org.opencontainers.image.title="trid3nt-telemac-worker" \
      org.opencontainers.image.description="TRID3NT TELEMAC worker: a staged run directory in, a solved run directory out (telapy-driven case solve plus the in-worker agitation and stratified builders)." \
      org.opencontainers.image.licenses="GPL-3.0"

# --- pinned engine stack (conda for opentelemac + the numpy ABI pin) ---------
# opentelemac carries its own numpy; numpy is pinned in the SAME solve so the
# _hermes C ext stays ABI-valid. pip (below) then installs into the same env
# WITHOUT upgrading it.
ARG OPENTELEMAC_VERSION=v9.0.0
ARG NUMPY_VERSION=1.26.4
RUN micromamba install -y -n base -c conda-forge \
        "opentelemac=${OPENTELEMAC_VERSION}" \
        "numpy=${NUMPY_VERSION}" \
        pip \
    && micromamba clean --all --yes

# Activate the base env for every subsequent RUN, so $HOMETEL/$SYSTELCFG and the
# solver scripts resolve and pip targets the conda env's python.
ARG MAMBA_DOCKERFILE_ACTIVATE=1

# --- geo deps via PyPI (numpy re-pinned so pip cannot pull it to 2.x) --------
RUN python -m pip install --no-cache-dir \
        "numpy==1.26.4" \
        "rasterio==1.3.11" \
        "pyproj==3.7.2" \
        "scipy==1.17.1" \
    && python -c "import numpy,scipy,rasterio,pyproj; \
print('geo deps OK numpy', numpy.__version__, 'rasterio', rasterio.__version__)" \
    && python -c "import numpy; assert numpy.__version__=='1.26.4', 'numpy pin drifted: '+numpy.__version__" \
    && python -c "from data_manip.extraction.telemac_file import TelemacFile; print('TELEMAC _hermes import OK')"

# --- the RAINDEF=3 user fortran, baked once from the installed engine source --
# The engine's own runoff_scs_cn.f already implements the block-type hyetograph
# branch but fixes RAINDEF as a compile-time PARAMETER, which no steering keyword
# can reach; the engine's user-fortran door is the only way in, and a run that
# wants it names this directory as FORTRAN FILE so telemac2d recompiles the copy
# over the library symbol. The copy changes that ONE parameter and nothing else.
# The expected line must be present VERBATIM or the build stops here: an
# unpatched copy would solve constant rain while the run reports a hyetograph.
USER root
RUN gate='      INTEGER, PARAMETER ::RAINDEF=1'; \
    src="$HOMETEL/sources/telemac2d/runoff_scs_cn.f"; \
    n=$(grep -cxF "$gate" "$src" || true); \
    if [ "$n" != "1" ]; then \
        echo "RAINDEF BAKE REFUSED: $src carries $n copies of '$gate' (expected exactly 1). The installed engine source has drifted and the baked user fortran would be a mis-patch." >&2; \
        exit 1; \
    fi; \
    mkdir -p /opt/trid3nt/user_fortran/raindef3; \
    sed "s|^${gate}\$|      INTEGER, PARAMETER ::RAINDEF=3|" "$src" \
        > /opt/trid3nt/user_fortran/raindef3/runoff_scs_cn.f; \
    grep -qxF '      INTEGER, PARAMETER ::RAINDEF=3' \
        /opt/trid3nt/user_fortran/raindef3/runoff_scs_cn.f; \
    echo "raindef3 user fortran baked from $src"

# --- the worker package ------------------------------------------------------
# The entrypoint imports its siblings off the script dir, so they all land in the
# same directory.
COPY --chown=root:root . /opt/trid3nt/workers/telemac/

# Root so the container can write the bind-mounted /data, which is owned by the
# host launcher user. The conda base env stays active.
USER root

ENV PYTHONUNBUFFERED=1 \
    TRID3NT_TELEMAC_DATA_DIR=/data \
    OMPI_ALLOW_RUN_AS_ROOT=1 \
    OMPI_ALLOW_RUN_AS_ROOT_CONFIRM=1
# opentelemac launches a solver via ``mpirun --use-hwthread-cpus -np 1``, and
# OpenMPI's prterun refuses to run as root; these two are OpenMPI's sanctioned
# override for a single-rank containerized root solve.

WORKDIR /data

# Build-time smoke: the entrypoint and both in-worker builders import under the
# activated env, the four telapy classes a case may name resolve, each builder's
# config maps through the one strict gate, and the baked user fortran is on disk.
# A path/import/pin mistake surfaces at BUILD time instead of mid-solve.
RUN python -c "\
import sys, pathlib;\
sys.path.insert(0, '/opt/trid3nt/workers/telemac');\
import entrypoint as E;\
from telapy.api.t2d import Telemac2d;\
from telapy.api.t3d import Telemac3d;\
from telapy.api.wac import Tomawac;\
from telapy.api.art import Artemis;\
import artemis_build as A, telemac3d_build as T;\
agitation = E._strict_section('agitation', {'wave_mode': 'resonance', 'wave_period_s': 32.0}, E._config_fields(A.ArtemisConfig));\
stratified = E._strict_section('stratified', {'flow_mode': 'wind_circulation', 'nplan': 11}, E._config_fields(T.Telemac3dConfig));\
assert A.ArtemisConfig(**agitation).wave_period_s == 32.0;\
assert T.Telemac3dConfig(**stratified).nplan == 11;\
assert sorted(E._MODULES) == ['artemis', 'telemac2d', 'telemac3d', 'tomawac'];\
assert pathlib.Path('/opt/trid3nt/user_fortran/raindef3/runoff_scs_cn.f').is_file();\
print('telemac worker OK: parser', E._PARSER_VERSION, '| telapy modules, config maps and baked raindef3 all present')"

# The negative half of the same gate, in a SEPARATE process: an unknown case
# field must refuse loudly. A dropped key silently no-ops the knob the caller
# meant to set, which reads afterwards as a solve that ignored its input rather
# than as the typo or the stale image it was. The output is captured and grepped
# so an unrelated crash cannot pass for the refusal.
RUN (python -c "\
import sys;\
sys.path.insert(0, '/opt/trid3nt/workers/telemac');\
import entrypoint as E;\
E._strict_section('case', {'module': 'telemac2d', 'bogus_key': 1}, E._CASE_FIELDS)" \
        > /tmp/strict_gate.log 2>&1; echo "exit=$?" >> /tmp/strict_gate.log) \
    && grep -q "^exit=1$" /tmp/strict_gate.log \
    && grep -q "UnknownManifestFieldError" /tmp/strict_gate.log \
    && rm -f /tmp/strict_gate.log \
    && echo "telemac strict gate OK: an unknown case field refused"

# One-shot solver: the activation entrypoint sources the base env, then execs the
# worker. The launcher appends nothing, so the entrypoint reads /data/manifest.json
# and writes its outputs into /data.
ENTRYPOINT ["/usr/local/bin/_entrypoint.sh"]
CMD ["python", "/opt/trid3nt/workers/telemac/entrypoint.py"]
