#!/usr/bin/env bash
# Package the QGIS plugin into the daemon-served custom repository.
#
# Builds qgis-plugin/trid3nt into a versioned zip + regenerates plugins.xml +
# manifest.json under the served directory (run/plugin-repo/ by default, or
# $TRID3NT_PLUGIN_REPO_DIR). Wired into `make agent` so every deploy refreshes
# the served artifact. The actual packaging logic lives in
# trid3nt_server.plugin_repo (single source of truth, reused by the serve
# routes); this script is the deploy-time entrypoint.
#
# Version is metadata.txt-driven and NEVER auto-bumped: a code change without a
# version= bump prints a WARNING here (Plugin Manager would not see the update)
# but does not fail the build.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${REPO_ROOT}/venvs/agent/bin/python"
if [ ! -x "${PY}" ]; then
  PY="python3"
fi

TRID3NT_REPO_ROOT="${REPO_ROOT}" "${PY}" - <<'PYEOF'
import sys
from trid3nt_server import plugin_repo

info = plugin_repo.package_plugin_repo()
print(
    f"plugin-repo: packaged {info['zip_filename']} (version={info['version']}) "
    f"-> {info['served_dir']}"
)
if info["warned"]:
    print(
        "plugin-repo: WARNING tree changed but version was not bumped -- "
        "bump version= in qgis-plugin/trid3nt/metadata.txt",
        file=sys.stderr,
    )
PYEOF
