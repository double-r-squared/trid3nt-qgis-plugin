# The PEST++ worker image: one staged study directory in, one finished study
# directory out.
#
# The launcher bind-mounts a run directory at /data (``docker run -v
# <rundir>:/data -w /data``) carrying manifest.json, the PEST++ control file,
# its template and instruction files and the model files every trial runs. The
# entrypoint copies that directory once per agent into the container's own
# /work, starts one PEST++ manager in /data and the agents on localhost, and
# writes pestpp_metrics.json back beside the manager's own record. The
# container does no network and no object-store I/O: the supervisor uploads
# whatever it wrote.
#
# ENGINE + PIN DISCIPLINE:
#   * PEST++ 5.2.27 is the linux release tarball of github.com/pestpp/pestpp
#     (a prerelease; every release since 5.2.16 is), checksum-pinned. Its
#     executables link only libstdc++, libgcc, libm and libc, which slim carries.
#   * mf6 6.7.0 is the SAME static release zip and checksum the MODFLOW worker
#     installs (workers/modflow/Dockerfile): one source for the model the
#     trials run.
#   * pyEMU 1.7.0 and its two runtime requirements, numpy and pandas, pinned:
#     a PstFrom interface's forward_run.py imports pyemu in every trial to
#     apply its multipliers before the model runs.
# A version skew here is a deliberate, IaC-visible bump.
#
# Build context = THIS worker dir, not the repo root:
#   docker build -t trid3nt-local/pestpp:latest workers/pestpp/

FROM python:3.11-slim

LABEL org.opencontainers.image.title="trid3nt-pestpp-worker" \
      org.opencontainers.image.description="TRID3NT PEST++ worker: a staged study directory in, a finished study directory out (one PEST++ manager and its agents on localhost)."

ARG PESTPP_VERSION=5.2.27
ARG PESTPP_SHA256=966547108168c63a28f08c8660f9cb9f12b02192488e0a7f4f055ef3db3ba37b
ARG MF6_VERSION=6.7.0
ARG MF6_SHA256=f83e743bac1b3009836a44e6e4aa4b4c522ac2e2c7d291e280db47f68353ebdc

# Fetch, checksum-verify and install PEST++ and mf6, then drop the fetch tooling
# from the layer. The zip is extracted with stdlib zipfile: slim has no unzip.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -fsSL \
        "https://github.com/pestpp/pestpp/releases/download/${PESTPP_VERSION}/pestpp-${PESTPP_VERSION}-linux.tar.gz" \
        -o /tmp/pestpp.tar.gz \
    && echo "${PESTPP_SHA256}  /tmp/pestpp.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/pestpp.tar.gz -C /usr/local \
    && rm -f /tmp/pestpp.tar.gz \
    && curl -fsSL \
        "https://github.com/MODFLOW-USGS/modflow6/releases/download/${MF6_VERSION}/mf${MF6_VERSION}_linux.zip" \
        -o /tmp/mf6.zip \
    && echo "${MF6_SHA256}  /tmp/mf6.zip" | sha256sum -c - \
    && python -c "\
import zipfile;\
z = zipfile.ZipFile('/tmp/mf6.zip');\
names = [n for n in z.namelist() if n.rsplit('/', 1)[-1] == 'mf6'];\
assert names, 'mf6 binary not in zip';\
open('/usr/local/bin/mf6', 'wb').write(z.read(names[0]))" \
    && chmod +x /usr/local/bin/mf6 \
    && rm -f /tmp/mf6.zip \
    && apt-get purge -y curl && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/* \
    && mf6 --version \
    && for tool in glm ies sen swp opt mou da sqp; do test -x /usr/local/bin/pestpp-$tool; done

RUN pip install --no-cache-dir pyemu==1.7.0 numpy==2.3.1 pandas==2.3.3 \
    && python -c "import pyemu; assert pyemu.__version__ == '1.7.0', pyemu.__version__"

# --- the worker package ------------------------------------------------------
COPY --chown=root:root entrypoint.py /opt/trid3nt/workers/pestpp/entrypoint.py

ENV PYTHONUNBUFFERED=1 \
    TRID3NT_PESTPP_DATA_DIR=/data

WORKDIR /data

# Build-time smoke: the entrypoint imports, its parser version and dispatch are
# the ones this image was built against, and the strict gate REFUSES an unknown
# case field. The negative half runs in a separate process and is grepped, so
# an unrelated crash cannot pass for the refusal.
RUN python -c "\
import sys;\
sys.path.insert(0, '/opt/trid3nt/workers/pestpp');\
import entrypoint as E;\
assert E._PARSER_VERSION == 'pestpp-case-1', E._PARSER_VERSION;\
assert sorted(E._DISPATCH) == ['case'];\
assert E._completed(__import__('pathlib').Path('/tmp')) is False;\
print('pestpp worker OK: parser', E._PARSER_VERSION, '| one case dispatch present')"
RUN (python -c "\
import sys;\
sys.path.insert(0, '/opt/trid3nt/workers/pestpp');\
import entrypoint as E;\
E._strict_section('case', {'module': 'ies', 'bogus_key': 1}, E._CASE_FIELDS)" \
        > /tmp/strict_gate.log 2>&1; echo "exit=$?" >> /tmp/strict_gate.log) \
    && grep -q "^exit=1$" /tmp/strict_gate.log \
    && grep -q "UnknownManifestFieldError" /tmp/strict_gate.log \
    && rm -f /tmp/strict_gate.log \
    && echo "pestpp strict gate OK: an unknown case field refused"

CMD ["python", "/opt/trid3nt/workers/pestpp/entrypoint.py"]
