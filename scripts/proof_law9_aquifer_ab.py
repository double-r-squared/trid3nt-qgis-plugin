"""Live A/B proof for law 9 P2 -- the Woburn TCE plume, aquifer K resolve-or-refuse.

(A) under-specified auto run, SoilGrids cannot serve -> the typed PHYSICS_INPUT_REQUIRED
    refusal naming aquifer_k_ms + porosity (law 9: never solve on an invented demo K).
(B) the SAME prompt with SoilGrids resolution on -> a real solve whose K/porosity
    provenance reads derived / fetch_soilgrids at the AOI.

Run with the local env sourced (TRID3NT_MODFLOW_LOCAL=1, MinIO block, mf6 bin).
"""
from __future__ import annotations

import asyncio
import sys

from trid3nt_server.data import TOOL_REGISTRY
from trid3nt_server.workflows.shared import aquifer_resolve
from trid3nt_server.workflows.shared.aquifer_resolve import (
    resolve_aquifer_properties,
)
from trid3nt_server.workflows.modflow.contaminant_plume.contaminant_plume import (
    ContaminantPlumeScenarioError,
    model_contaminant_plume,
)

PLACE = "Woburn, Massachusetts"
SPECIES = "trichloroethylene"  # the real Woburn wells G & H contaminant (TCE)
RATE = 0.02  # kg/s screening release
DAYS = 30.0


def _geocode(place: str) -> tuple[float, float]:
    fn = TOOL_REGISTRY["geocode_location"].fn
    r = fn(place)
    lat = r.get("latitude") if isinstance(r, dict) else getattr(r, "latitude", None)
    lon = r.get("longitude") if isinstance(r, dict) else getattr(r, "longitude", None)
    return float(lat), float(lon)


async def main() -> int:
    print("=" * 78)
    print(f"LAW-9 P2 LIVE A/B  -- {PLACE} / {SPECIES}")
    print("=" * 78)

    lat, lon = _geocode(PLACE)
    print(f"\n[geocode] {PLACE} -> lat={lat:.5f} lon={lon:.5f}")

    # --- provenance probe: what does the shared resolver derive at the AOI? -----
    print("\n--- resolver provenance at the AOI (SoilGrids-derived) ---")
    res = await resolve_aquifer_properties(lat, lon, None, None)
    print(f"resolved         : {res.resolved}")
    print(f"k_ms             : {res.k_ms}")
    print(f"porosity         : {res.porosity}")
    print(f"k_source         : {res.k_source}")
    print(f"porosity_source  : {res.porosity_source}")
    print(f"soil_meta        : {res.soil_meta}")
    for e in res.entries:
        print(f"  entry[{e.param}] basis={e.basis} consequence={e.consequence} "
              f"value={e.value} real_source={e.real_source_if_any}")
        print(f"    note: {e.note}")
    dead_constant = 1e-4  # the DELETED DEFAULT_AQUIFER_K_MS demo value
    if res.k_ms is not None:
        ratio = res.k_ms / dead_constant
        print(f"\n[compare] derived K={res.k_ms:.3e} m/s vs DEAD demo constant "
              f"{dead_constant:.0e} m/s  (ratio {ratio:.2f}x)")

    # --- (A) under-specified auto run with SoilGrids UNABLE to serve -> REFUSE ---
    print("\n" + "-" * 78)
    print("(A) UNDER-SPECIFIED AUTO RUN -- SoilGrids off-coverage/unavailable")
    print("-" * 78)
    orig_derive = aquifer_resolve.derive_soil_k

    def _no_soil(_lat: float, _lon: float):
        return None, {"reason": "AOI outside SoilGrids coverage (A/B: forced unavailable)"}

    aquifer_resolve.derive_soil_k = _no_soil  # type: ignore[assignment]
    a_outcome = ""
    try:
        await model_contaminant_plume(
            location=PLACE, contaminant=SPECIES, release_rate_kg_s=RATE,
            duration_days=DAYS, input_mode="auto",
        )
        a_outcome = "NO REFUSAL (law-9 FAILURE)"
        print("!! (A) did NOT refuse -- law 9 violated")
    except ContaminantPlumeScenarioError as exc:
        a_outcome = str(exc)
        print(f"(A) refused as expected:\n{a_outcome}")
    finally:
        aquifer_resolve.derive_soil_k = orig_derive  # type: ignore[assignment]

    a_ok = ("PHYSICS_INPUT_REQUIRED" in a_outcome and "aquifer_k_ms" in a_outcome)
    print(f"\n(A) verdict: {'PASS' if a_ok else 'FAIL'} "
          f"(typed refusal names aquifer_k_ms)")

    # --- (B) same prompt, SoilGrids resolution on -> real solve -----------------
    print("\n" + "-" * 78)
    print("(B) SAME PROMPT, SoilGrids resolution ON -> real solve")
    print("-" * 78)
    b_ok = False
    try:
        result = await model_contaminant_plume(
            location=PLACE, contaminant=SPECIES, release_rate_kg_s=RATE,
            duration_days=DAYS, input_mode="auto",
        )
        summary = getattr(result, "summary", {}) or {}
        plumes = getattr(result, "plumes", None)
        n_plumes = len(plumes) if plumes is not None else "n/a"
        print(f"(B) SOLVE OK -- plumes={n_plumes}")
        print(f"    aquifer_provenance: {summary.get('aquifer_provenance')}")
        print(f"    location_name     : {summary.get('location_name')}")
        if plumes:
            p0 = plumes[0]
            print(f"    plume[0] name     : {getattr(p0, 'name', None)}")
            print(f"    plume[0] uri      : {getattr(p0, 'uri', None)}")
            print(f"    plume[0] max_conc : {getattr(p0, 'max_concentration', None)}")
        prov = str(summary.get("aquifer_provenance") or "")
        b_ok = ("SoilGrids" in prov or "pedotransfer" in prov)
    except Exception as exc:  # noqa: BLE001 -- report whatever happened
        print(f"(B) FAILED: {type(exc).__name__}: {exc}")

    print(f"\n(B) verdict: {'PASS' if b_ok else 'FAIL'} "
          f"(K/porosity provenance reads derived:soilgrids)")

    print("\n" + "=" * 78)
    print(f"A/B RESULT: A={'PASS' if a_ok else 'FAIL'}  B={'PASS' if b_ok else 'FAIL'}")
    print("=" * 78)
    return 0 if (a_ok and b_ok) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
