# grace2-contracts

Shared contracts for this system — the WebSocket protocol, the `AssessmentEnvelope`,
`EventMetadata` + `ClaimSet`/`NumericClaim`, the five MongoDB collection schemas,
`CatalogEntry`, and the solver-execution shapes
(`ModelSetup`/`RunResult`/`ExecutionHandle`/`LayerURI`).

Single source of truth for every type that crosses a specialist boundary:
`web` ↔ `agent` ↔ `engine` ↔ `infra` ↔ `testing`. Pydantic v2; SRS v0.3
Appendices A–D are the authoritative starting stubs (the SRS itself is the
user's document; appendix amendments flow through the schema specialist's
report rather than being edited in place).

## Modules

| Module | What | SRS reference |
|---|---|---|
| `common` | `GraceModel`, `ULIDStr`, `BBox`, `TimeRange`, datetime + UTC serialization | A.1, B.7, D.7 |
| `ws` | WebSocket envelope + every A.3/A.4/A.4b message type + A.6 error codes | Appendix A, FR-AS-5 |
| `envelope` | `AssessmentEnvelope`, supporting types, `FloodPayload` + `FloodMetrics` | Appendix B, FR-TA-1, FR-AS-7 |
| `event` | `EventMetadata`, `EventLocation`, intensity discriminated union, `NumericClaim` + `ClaimSet` | Appendix C, FR-HEP-5, Decision M |
| `collections` | Five MongoDB collection models + vector index configs + TTL config | Appendix D, FR-MP-5, Decision F/L |
| `catalog` | `CatalogEntry` for `public_hazard_catalog.yaml` | FR-PHC-2 |
| `execution` | `ModelSetup`, `RunResult`, `ExecutionHandle` (Cloud Workflows execution-id cancellation seam), `LayerURI` | FR-TA-2, FR-CE-2/3, FR-AS-6 |
| `tool_metadata` | Tool-docstring conventions + `tool_category` vocabulary (convention only; `agent` owns the registry code) | FR-TA-3, FR-AS-3 |
| `export_schemas` | CLI / `grace2-export-schemas` script that writes JSON Schemas for every top-level contract | — |

## Install (development)

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e contracts
pip install pytest  # for tests
```

The package targets Python `>= 3.11`. Pydantic `>= 2, < 3`; `python-ulid`
`>= 2, < 4`.

## Run the round-trip tests

```bash
pytest contracts/tests -v
```

Every WebSocket message type (Appendix A.3, A.4, A.4b), the
`AssessmentEnvelope`, every per-event-type `IntensityIndicators` payload, every
`MongoDB` collection, `CatalogEntry`, and every solver-execution shape is
exercised through a real `JSON -> model -> JSON` round-trip with idempotence
checks. Negative controls include: bare-float intensity rejection (Decision M),
wrong-subtype-for-`hazard_type` rejection, missing-bbox-and-place_name
rejection, no-cost-field assertions on `confirmation-request` /
`RunDocument` / `FloodMetrics`, invalid ULID rejection, inverted-bbox
rejection, and the discriminated-union dispatcher invariant on `EventMetadata`.

## Regenerate JSON Schemas

```bash
# Default output: contracts/schemas/
grace2-export-schemas

# Or to a custom directory
grace2-export-schemas contracts/schemas
```

Output is sorted and `\n`-terminated so re-runs against an unchanged contract
set produce byte-identical files (`git diff` is the drift signal).

## Wire form

`model.model_dump(mode="json")` is the canonical wire form. For documents that
use the Mongo `_id` alias (every `DocModel` subclass), use
`model.model_dump(**MONGO_DUMP_KWARGS)` which is `mode="json", by_alias=True`.

Datetimes serialize to ISO-8601 with a `Z` suffix (UTC). ULIDs are 26-char
strings. `bbox` is always `[minLon, minLat, maxLon, maxLat]` in EPSG:4326.
`payload` is always an object (`{}` when empty).

## Versioning

Each top-level document carries a `schema_version: Literal["v1"]` first field.
Additive growth (new optional fields, new `Literal` members for open enums) is
preferred; a breaking change bumps the version. The enums most likely to grow
(`hazard_type`, `event_type`, `tool_category`, forcing-source type) are open by
design so new engines register members without a breaking change (SRS Decision
G; AGENTS.md "Pre-MVP scope" — no backward-compatibility shims).

## Amendments to SRS Appendices A–D

The SRS appendices are stubs and are **expected to drift** as implementation
surfaces gaps. The schema specialist never edits the SRS; instead, appendix
amendments are surfaced in each `report.md` for the user to land. See the
job-0013 report's *Amendment Log* for the full list, including the
`research_mode` field on `user-message` (FR-WC-15) and minor structural notes.
