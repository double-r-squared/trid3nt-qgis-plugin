# HEC-RAS 6.6 pure-2D forcing reference -- the CLEAN, dam-free fake-reach form (OI-FT1)

The shipped **clean pure-2D reference deck** ADR 0136 recorded as not existing (it
said the shipped `x09` could not be reduced to a dam-free reach without a
node-topology rebuild). It DOES ship -- in a different project. Extracted from
HEC's SHA-pinned `Example_Projects_6_6.zip`
(sha256 `ea239b506155a2dfeda2af80b3c2af948eef42c40218bcd65de472cfed386887`,
432389121 B -- the same distribution ADR 0125/0132/0134 use),
`2D Sediment Transport/Chippewa_2D/`. Public-domain USACE software; CRLF stripped.

## Why this supersedes the ADR 0134/0135 `b06` (BaldEagle) reference

The `.bNN` serialization of a 2D-BC-line inflow **changed between HEC-RAS versions**:

| reference | RAS version | 2D-BC-line inflow header in the `.bNN` |
| --- | --- | --- |
| `BaldEagleDamBrk.b06` (ADR 0134/0135) | **6.2.0** | **BARE** `Upstream Flow Hydrograph` (no suffix) |
| `Chippewa_2D.b02` (this dir) | **6.4** | `Upstream Flow Hydrograph - River: Fake River  Reach: Fake Reach  RS: 100` |
| our solver image | **6.6** | the Chippewa (fake-reach) form |

This is the root cause of the ADR 0136 STOP: it used the 6.2 `b06` BARE form as the
reference for a **6.6** engine. In 6.6 a bare `Upstream Flow Hydrograph` maps
positionally to the 1D reach (as ADR 0136 observed), NOT to a 2D BC line. The 6.6
form is the **suffixed fake-reach** header -- `River: Fake River  Reach: Fake Reach
RS: 100`. ADR 0134/0135's "bare == 2D BC line" was correct for 6.2 and wrong for 6.6.

## Why Chippewa is the CLEAN reference (the x09-dam blocker dissolved)

`Chippewa_2D.x01` is a single-2D-area deck with:

* `Section - Storage Area Data`: `SA  8 ... Perimeter 1` (the 2D area as an SA),
  perimeter-point count in the 4th field of the next line (`0 0 0 39 T`);
* `Section - Storage Area Connection Data`: **EMPTY** -- NO dam, NO gate, NO SA
  connection, NO lateral weir (the entanglement that made `x09` irreducible);
* `Section - River Reach Data`: a minimal `Fake River`/`Fake Reach` -- 2 cross
  sections (RS 100, RS 0) on a 100 ft x 10 ft dummy box, trivial coordinates (NOT
  on the real BC line -- so the fake reach is a REQUIRED DUMMY, not spatially
  coupled);
* `Section - Arrays Sizes`: rows A/B are `1 1 0 2 8 F` / `3 3 0 0 0 0 0 0` -- and
  they are **byte-identical to Weise's** (a different mesh: 9-pt SA vs Chippewa's
  39). Proof the Arrays-Sizes A/B rows encode the fake reach (2 XS), NOT the 2D
  cell/perimeter count. Grafting a new SA name + perimeter count is therefore
  Arrays-Sizes-safe (the fix ADR 0136's `x09` graft lacked).

## Files

| file | what it is |
| --- | --- |
| `Chippewa_2D.x01` | the Linux `.xNN` geometry-preprocessor: SA + clean Fake River/Fake Reach, empty SA-connection. THE clean reference. |
| `Chippewa_2D.b02` | the Linux `.bNN`: `Upstream Flow Hydrograph - River: Fake River  Reach: Fake Reach  RS: 100` (2 ordinates) + `Downstream Normal Depth`. NOTE `HYDROGRAPH LOCATIONS = 0` -> divide-by-zero unless pointed at 1 node. |
| `g01_bc_lines_schema.json` | the `.g01.hdf` `/Geometry/Boundary Condition Lines/` schema -- Attributes `[Name S32, SA-2D S16, Type S8, Length f4]` (Type `External` for BOTH inflow + outflow), External Faces, Polyline Info/Parts/Points. IDENTICAL to what `hecras_geometry_writer.write_boundary_condition_lines` authors. |

## The decoded grammar (Rosetta, from the sweep of ALL shipped Linux intermediates)

The full `Example_Projects_6_6` sweep found 29 `.bNN` / 26 `.xNN`. Four projects
carry a single 2D area with Linux intermediates: BaldEagle (6.2, combined with a
dam), **Chippewa** + **Weise** (6.4 sediment, clean fake reach), Davis (Pipes).
ALL of Chippewa/Weise/Davis force the 2D area with the SUFFIXED fake-reach header
(never bare). The `.bNN` "Hydrograph Data" flag lines are IDENTICAL between a 1D
reach inflow (Muncie `b04`) and a 2D-BC-line inflow (Chippewa `b02`): the ONLY
`.bNN` difference is the header SUFFIX -- `River: <real> Reach: <real> RS: <real>`
(1D) vs `River: Fake River Reach: Fake Reach RS: 100` (2D BC line).

## The one remaining link (the OI-FT1 wall, precisely bracketed)

A 2D-BC-line inflow is a **2D external boundary condition**, read by the engine's
`read_un_q2d_bc_` (Read_UN_Q2D_BC.for), from the **plan-HDF `/Event Conditions`**.
The fake reach's flow does NOT route into the 2D area by itself (proven: the
Chippewa-form carve deck SOLVES clean at vol err 0.0 but the flow passes 1D
in->out through the fake reach and the 2D area stays DRY -- `GeomPreprocess/Reach
Connections` shows the fake reach standalone, `NDCON = 0`, `IDSTYP = 0`). Wetting
needs the 2D BC line ENUMERATED as a 2D flow boundary in the plan-HDF Event
Conditions. **No shipped file in this distribution exposes that schema** -- not
Chippewa's `u04.hdf` (only empty Meteorology), not the lone Water-Quality
`.p01.hdf` (not a 2D-BC deck), not the Muncie solved fixture (1D reach only).
Authoring it blind SEGFAULTS in `read_un_q2d_bc_` (the ADR 0133/0136 "no blind
authoring" rule, empirically re-confirmed). That plan-HDF 2D-BC Event-Conditions
schema is the precise OI-FT1 STOP.
