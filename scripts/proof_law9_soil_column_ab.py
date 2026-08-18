"""Live A/B proof for law 9 P3 -- the SWMM two-zone aquifer baseflow soil column.

The cheapest wired P3 template (swmm_aquifer_baseflow_to_node: in-process pyswmm,
no DEM / no worker image), at a real CONUS site.

(A) SoilGrids resolution ON -> the two-zone [AQUIFERS] moisture column
    (porosity / wilting / field capacity / conductivity) is DERIVED from the AOI
    texture (Saxton-Rawls) and the pyswmm baseflow solve completes with the derived
    provenance visible.
(B) SoilGrids force-failed -> the typed SWMM_PHYSICS_INPUT_REQUIRED refusal, no
    solve on an invented column (law 9).

Run with the local env sourced (MinIO block; pyswmm installed).
"""
from __future__ import annotations

import asyncio
import sys

from trid3nt_server.workflows.swmm.aquifer_baseflow import aquifer_baseflow as mod

PLACE = "Ames, Iowa"  # deep agricultural soil - clear SoilGrids texture coverage


async def main() -> int:
    print("=" * 78)
    print(f"LAW-9 P3 LIVE A/B  -- swmm_aquifer_baseflow_to_node @ {PLACE}")
    print("=" * 78)

    # --- (A) derived-solve: SoilGrids serves the two-zone column ----------------
    print("\n" + "-" * 78)
    print("(A) SoilGrids resolution ON -> derive the column + pyswmm solve")
    print("-" * 78)
    a_ok = False
    result = await mod.swmm_aquifer_baseflow_to_node(
        location=PLACE, input_mode="auto", sim_days=24, a1=0.004,
    )
    if result.get("status") == "ok":
        col = result.get("aquifer_soil_column")
        print(f"(A) SOLVE OK -- baseflow_contribution_cfs="
              f"{result.get('baseflow_contribution_cfs')}")
        print(f"    aquifer_soil_column : {col}")
        print(f"    aquifer_provenance  : {result.get('aquifer_provenance')}")
        print(f"    recession_tau_hr    : {result.get('recession_tau_hr')}")
        print(f"    flow_routing_error% : {result.get('flow_routing_error_pct')}")
        prov = str(result.get("aquifer_provenance") or "")
        a_ok = ("DERIVED from SoilGrids" in prov and col is not None)
    else:
        print(f"(A) FAILED: {result.get('error_code')}: {result.get('error_message')}")
    print(f"\n(A) verdict: {'PASS' if a_ok else 'FAIL'} "
          f"(column DERIVED from SoilGrids + solve completes)")

    # --- (B) forced-unavailable refusal -----------------------------------------
    print("\n" + "-" * 78)
    print("(B) SoilGrids force-failed -> typed refusal, no solve")
    print("-" * 78)
    orig = mod.derive_soil_column
    mod.derive_soil_column = (  # type: ignore[assignment]
        lambda lat, lon: (None, {"reason": "AOI off SoilGrids coverage (A/B forced)"})
    )
    b_ok = False
    try:
        refusal = await mod.swmm_aquifer_baseflow_to_node(
            location=PLACE, input_mode="auto", sim_days=24,
        )
    finally:
        mod.derive_soil_column = orig  # type: ignore[assignment]
    print(f"(B) status={refusal.get('status')} code={refusal.get('error_code')}")
    print(f"    message: {str(refusal.get('error_message'))[:220]}")
    b_ok = (
        refusal.get("status") == "error"
        and refusal.get("error_code") == "SWMM_PHYSICS_INPUT_REQUIRED"
        and "aquifer_soil_column" in str(refusal.get("error_message"))
    )
    print(f"\n(B) verdict: {'PASS' if b_ok else 'FAIL'} "
          f"(typed SWMM_PHYSICS_INPUT_REQUIRED, no invented column)")

    print("\n" + "=" * 78)
    print(f"A/B RESULT: A={'PASS' if a_ok else 'FAIL'}  B={'PASS' if b_ok else 'FAIL'}")
    print("=" * 78)
    return 0 if (a_ok and b_ok) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
