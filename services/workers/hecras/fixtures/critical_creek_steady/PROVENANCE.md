# critical_creek_steady -- HEC-RAS Applications Guide Example 1 (Critical Creek)

## What this is

The other 1D STEADY-flow example ADR 0170's recipe named by name -- HEC's
"Applications Guide Example 1", the smallest/oldest of the canonical steady
teaching cases (2 geometries, 2 flow files, existing vs. modified conditions).
Seeded alongside `beaver_creek_steady` for breadth (a second, independent
steady project) for ADR 0170.

## Source

Same zip as `beaver_creek_steady` -- see that fixture's `PROVENANCE.md` for
the full URL/SHA-256/license block (repeated here for a self-contained
record):

- `https://github.com/HydrologicEngineeringCenter/hec-downloads/releases/download/1.0.45/Example_Projects_7_0.zip`
  (linked from `https://www.hec.usace.army.mil/software/hec-ras/download.aspx`,
  "HEC-RAS 7.0 Example Projects")
- SHA-256 `fac04f071e624c841b20e943e3b68f351b4531f565a0c24ab7d885cf9e38d523`
  (427,304,097 bytes)
- Path inside the zip: `Applications Guide/Example 1 - Critical Creek/`
- Public domain, U.S. Federal Government work (USACE HEC), same terms as
  `muncie_smoke`.

## Contents (verbatim, nothing trimmed -- the whole example is 342 KB)

| file | role |
| --- | --- |
| `CRITCREK.prj` | project file -- 2 plans, `Geom File=g01/g02`, `Flow File=f01/f02` (lowercase in the .prj text) |
| `CRITCREK.g01`, `.g02` | 1D geometry text -- **NO computed HDF ships with this project** (older/legacy example; predates HEC-RAS writing geometry HDF for every project) |
| `CRITCREK.F01`, `.F02` | steady flow text (note the actual file names on disk are UPPERCASE `F01`/`F02` even though the `.prj`/`.pNN` text reference lowercase `f01`/`f02` -- a real case-sensitivity trap on a Linux filesystem, documented as-shipped, not renamed) |
| `CRITCREK.p01`, `.p02` | plan text (`Program Version=5.00` / `4.01` -- genuine steady plans) |
| `CRITCREK.O01`, `.O02` | **legacy BINARY output files** (pre-HDF DOS-era results format -- not ASCII, not parsed by this job; kept for completeness/provenance only) |
| `CRITCREK.S01`, `.S02` | empty summary-file placeholders (0 bytes, as shipped) |

## Plans

| plan | title | geom | flow |
| --- | --- | --- | --- |
| p01 | Existing Conditions | g01 | f01 (file: `F01`) |
| p02 | Modified Geometry Conditions | g02 | f02 (file: `F02`) |

See `schema_notes.md` for the VERIFY results.
