#!/usr/bin/env bash
# HEC-RAS 2025 Beta headless-characterization probe (ADR 0127 spike).
#
# Runs the baked managed `ras` CLI on Linux and reports the spike verdict live:
#   1. `ras --version` + `ras --help`  -> the managed .NET 9 CLI is Linux-portable
#      (prints the full headless verb surface: createterrain/mesh/prepare/solve/map).
#   2. `ras healthcheck`               -> the NATIVE payload gap: the win-x64-only
#      public package ships no Linux natives (gdal_wrap.so / RasNativeParallel),
#      so a headless SOLVE is NOT yet possible -> NO-GO-YET.
#
# This is NOT a solver run. It is the reproducible characterization + the
# flip-ready host: the day HEC publishes the linux-x64 natives, swap them in
# beside the managed assemblies and `ras solve` runs headless unchanged.
set -uo pipefail

RAS_DLL="${RAS_DLL:-/opt/hecras2025/app/ras.dll}"
OUT_DIR="${TRID3NT_HECRAS2025_OUT_DIR:-/data}"
run() { dotnet "$RAS_DLL" "$@" 2>&1; }

echo "== HEC-RAS 2025 Beta -- Linux headless characterization probe =="
echo "-- ras --version --"
VERSION_OUT="$(run --version)"; echo "$VERSION_OUT"
echo "-- ras --help (headless verb surface) --"
run --help
echo "-- ras healthcheck (native dependency probe) --"
HEALTH_OUT="$(run healthcheck)"; echo "$HEALTH_OUT"

# Verdict: managed CLI ran if --version printed a build string; native payload is
# absent if healthcheck hit the multiplatform dlopen gap.
MANAGED_OK=false; case "$VERSION_OUT" in ras\ *) MANAGED_OK=true;; esac
NATIVE_OK=true
for m in "gdal_wrap" "RasNativeParallel" "cannot open shared object file" "GDAL failed to load"; do
  case "$HEALTH_OUT" in *"$m"*) NATIVE_OK=false;; esac
done

if [ -d "$OUT_DIR" ] && [ -w "$OUT_DIR" ]; then
  printf '{"managed_cli_runs": %s, "native_payload_present": %s, "go": %s, "version": "%s"}\n' \
    "$MANAGED_OK" "$NATIVE_OK" \
    "$([ "$MANAGED_OK" = true ] && [ "$NATIVE_OK" = true ] && echo true || echo false)" \
    "$(printf '%s' "$VERSION_OUT" | tr -d '\n')" > "$OUT_DIR/hecras2025_probe.json"
  echo "-- wrote $OUT_DIR/hecras2025_probe.json --"
fi

if [ "$MANAGED_OK" = true ] && [ "$NATIVE_OK" = false ]; then
  echo "VERDICT: NO-GO-YET -- managed CLI is Linux-portable; native payload is win-x64 only."
  exit 3   # honest, distinct non-zero: characterized gap, not a crash
fi
if [ "$MANAGED_OK" = true ] && [ "$NATIVE_OK" = true ]; then
  echo "VERDICT: GO -- native payload resolved on Linux; headless solve is now possible."
  exit 0
fi
echo "VERDICT: UNEXPECTED -- managed CLI did not run; investigate."
exit 1
