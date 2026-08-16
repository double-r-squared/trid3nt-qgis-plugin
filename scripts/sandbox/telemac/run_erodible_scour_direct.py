"""Direct-call driver for the GAIA v2 erodible-bed scour path: reruns the
showcase scour case (Snake River near Twin Falls, ID) straight through the
registered ``telemac_river_dye`` closure, no daemon/WS.

substance='scour' routes to the sediment class and, together with
erodible_bed=True, forces the GAIA erodible-bed coupling (the classification
gate and the erodible_bed gate share one source of truth). Pairs with
render_erodible_scour_proof.py, which renders the signed bed-evolution COG the
run publishes.

Run: venvs/agent/bin/python3 scripts/sandbox/telemac/run_erodible_scour_direct.py
"""
import asyncio
import json

from trid3nt_server.workflows.telemac.river_dye.river_dye import telemac_river_dye


async def main():
    result = await telemac_river_dye(
        location="Snake River near Twin Falls, Idaho",
        substance="scour",
        erodible_bed=True,
        morphological_factor=5.0,
        grain_size_um=300.0,
        bed_thickness_m=5.0,
        sim_duration_s=900,
    )
    out = result.model_dump() if hasattr(result, "model_dump") else result
    print(json.dumps(out, default=str, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
