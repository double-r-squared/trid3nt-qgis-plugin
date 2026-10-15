# The MODFLOW worker image: one manifest in, one solved run directory out.
#
# The launcher bind-mounts a run directory at /data (``docker run -v
# <rundir>:/data -w /data``) carrying manifest.json and every staged input.
# The entrypoint reads the manifest, runs mf6 once - optionally preceded by
# the engine's own ``--mode validate`` - and writes the results plus
# modflow_metrics.json back into the same directory. The container does no
# network and no object-store I/O: the supervisor uploads whatever it wrote.
#
# ENGINE + PIN DISCIPLINE:
#   * mf6 6.7.0 is the static linux binary from the modflow6 release, the same
#     pin scripts/fetch_binaries.sh installs on the host and says out loud must
#     never go below 6.7.0 (6.5.0 rejects PRP options the product writes).
#   * mf6 links only libc - no libmf6.so beside it, no fortran runtime to pin.
#   * PUBLIC DOMAIN (CC0): USGS's own release, so nothing of ours is GPL by
#     adjacency and the image carries no third-party Python at all - the
#     entrypoint is stdlib, and FloPy authors in the DAEMON, never here.
# A version skew here is a deliberate, IaC-visible bump.
#
# Build context = THIS worker dir, not the repo root:
#   docker build -t trid3nt-local/modflow:latest workers/modflow/

FROM python:3.11-slim

LABEL org.opencontainers.image.title="trid3nt-modflow-worker" \
      org.opencontainers.image.description="TRID3NT MODFLOW worker: a staged run directory in, a solved run directory out (mf6 validate/solve over mfsim.nam)." \
      org.opencontainers.image.licenses="CC0-1.0"

ARG MF6_VERSION=6.7.0
ARG MF6_SHA256=f83e743bac1b3009836a44e6e4aa4b4c522ac2e2c7d291e280db47f68353ebdc

# Fetch, checksum-verify and install the mf6 binary, then drop the fetch tooling
# from the layer. The zip is extracted with stdlib zipfile: slim has no unzip.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
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
    && mf6 --version

# --- the worker package ------------------------------------------------------
# The entrypoint is stdlib-only python; it lives beside nothing else.
COPY --chown=root:root entrypoint.py /opt/trid3nt/workers/modflow/entrypoint.py

ENV PYTHONUNBUFFERED=1 \
    TRID3NT_MODFLOW_DATA_DIR=/data

WORKDIR /data

# Build-time smoke: the entrypoint imports, its parser version and dispatch are
# the ones this image was built against, and the strict gate REFUSES an unknown
# case field. A path/import/pin mistake surfaces at BUILD time instead of
# mid-solve. The negative half runs in a separate process and is grepped, so an
# unrelated crash cannot pass for the refusal.
RUN python -c "\
import sys, pathlib;\
sys.path.insert(0, '/opt/trid3nt/workers/modflow');\
import entrypoint as E;\
assert E._PARSER_VERSION == 'modflow-case-1', E._PARSER_VERSION;\
assert sorted(E._DISPATCH) == ['case'];\
assert E._reached_normal_termination(pathlib.Path('/tmp')) is False;\
print('modflow worker OK: parser', E._PARSER_VERSION, '| one case dispatch present')"
RUN (python -c "\
import sys;\
sys.path.insert(0, '/opt/trid3nt/workers/modflow');\
import entrypoint as E;\
E._strict_section('case', {'module': 'gwf', 'bogus_key': 1}, E._CASE_FIELDS)" \
        > /tmp/strict_gate.log 2>&1; echo "exit=$?" >> /tmp/strict_gate.log) \
    && grep -q "^exit=1$" /tmp/strict_gate.log \
    && grep -q "UnknownManifestFieldError" /tmp/strict_gate.log \
    && rm -f /tmp/strict_gate.log \
    && echo "modflow strict gate OK: an unknown case field refused"

# One-shot solver: the entrypoint reads /data/manifest.json and writes its
# outputs into /data. The launcher appends nothing.
CMD ["python", "/opt/trid3nt/workers/modflow/entrypoint.py"]
