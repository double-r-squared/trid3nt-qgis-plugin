# pump_station -- HEC-RAS "Pumping Station" + "Pumping Station with Rules" examples

## What this is

Two related public HEC-RAS interior-drainage pump-station example projects:
a baseline pump station (fixed on/off trigger elevations) and a
rules-driven variant (a pump gated by a companion structure's opening state,
using HEC-RAS's `Rule Operation=`/`Rule Expression=` trigger scripting
language). Seeded for ADR 0171 row 3
(`pump_station_trigger_and_ramp_control`) and the related `gate_pump_rules`
board row.

## Source

- `https://github.com/HydrologicEngineeringCenter/hec-downloads/releases/download/1.0.45/Example_Projects_7_0.zip`
  (linked from `https://www.hec.usace.army.mil/software/hec-ras/download.aspx`,
  "HEC-RAS 7.0 Example Projects", 408 MB)
- SHA-256 `fac04f071e624c841b20e943e3b68f351b4531f565a0c24ab7d885cf9e38d523`
  (427,304,097 bytes)
- Paths inside the zip:
  - `1D Unsteady Flow Hydraulics/Pumping Station/` -> seeded as `Pumps.*`
  - `1D Unsteady Flow Hydraulics/Pumping Station with Rules/` -> seeded as
    `PumpRule.*` (that folder ALSO ships its own copy of a `Pumps.*`
    project with different dates/sizes than the standalone one; NOT
    seeded here since `PumpRule.p03` references its own `PumpRule.g01`/
    `PumpRule.u02`, not those `Pumps.*` files -- verified by grepping
    `Geom File=`/`Flow File=` in `PumpRule.p03`)
- Public domain, U.S. Federal Government work (USACE HEC), same terms as
  `muncie_smoke`.

## Contents (verbatim, nothing trimmed -- both projects together are 1.0 MB)

### `Pumps.*` (project title "Pump Station Example", Beaver Creek near
Kentwood LA -- same river as `beaver_creek_steady`, a different HEC teaching
case reusing the same watershed)

| file | role |
| --- | --- |
| `Pumps.prj` | project file, `Geom File=g01`, `Unsteady File=u01` |
| `Pumps.g01` | 1D geometry text -- carries a `Pump Station=` block (3 pumps, fixed on/off trigger elevations, an HQ head-discharge curve) |
| `Pumps.p01`, `.p02` | plan text ("Proposed Pumping Station" / "Test of thing") |
| `Pumps.u01` | unsteady flow forcing |
| `Pumps.c01`, `.dsc`, `.dss`, `.S01` | computation log / descriptor / DSS / summary placeholder |

No HDF of any kind ships with this project (pre-HDF vintage, like
`critical_creek_steady`).

### `PumpRule.*` (project title "Simple PUMP") -- the rules variant

| file | role |
| --- | --- |
| `PumpRule.prj` | project file, `Geom File=g01`, `Unsteady File=u02`, `Plan File=p03` |
| `PumpRule.g01` | 1D geometry text -- a DIFFERENT pump station (`PUMP STA #1`, river `RedFox`, 3 pumps with distinct on/off elevations) plus a companion inline gate structure the rules react to |
| `PumpRule.p03` | plan text ("Plan 03") |
| `PumpRule.u02` | unsteady flow forcing -- carries the real `Rule Operation=`/`Rule Expression=` trigger-logic block (see `schema_notes.md`) |
| `PumpRule.c01`, `.dss` | computation log / DSS |

No HDF ships with this project either.

## What was NOT seeded

Nothing was trimmed from either project -- both are small as shipped (the
whole `1D Unsteady Flow Hydraulics/Pumping Station*` tree in the zip is
under 2 MB combined). The duplicate `Pumps.*` copy inside the "with Rules"
folder was skipped as redundant/unreferenced (see above).

See `schema_notes.md` for the ASCII pump + rule-operation schema and the
VERIFY results.
