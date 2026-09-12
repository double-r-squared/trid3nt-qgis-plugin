# trid3nt-contracts

The types that cross a package boundary, defined once and imported everywhere:
the WebSocket protocol, the persisted document schemas, the data-source and
tool-registration declarations, and the result shapes an engine or a fetch
produces. Pydantic v2 throughout; every model subclasses `GraceModel`
(`extra="forbid"`, UTC-`Z` datetimes).

## Modules

| Module | What |
|---|---|
| `common` | `GraceModel`, `ULIDStr`, `BBox`, `TimeRange`, the UTC-`Z` datetime alias, the fallback and input-provenance records |
| `errors` | `ToolInputError` and the closed error-code and actionability vocabularies |
| `ws` | The WebSocket envelope, every message payload, the map-command args, and the type -> payload routing registry |
| `auth` | The two connect-handshake envelopes and the server-advertised sibling endpoints |
| `user` | The `User` account record |
| `case` | The Case envelopes: summary, persisted chat message, tool-card record, rehydration state, lifecycle command |
| `secrets` | Per-Case secret records and the just-in-time credential request/reply |
| `region_choice` | The region-narrowing picker request and its reply |
| `payload_warning` | The payload gate: the warning envelope, its confirmation, and the granularity, time-scale and param-sheet rows |
| `chart_contracts` | The `chart-emission` envelope, its Vega-Lite structural check, and the persisted chart record |
| `processing_contracts` | The session processing request/response pair and the code approval card |
| `envelope` | `AssessmentEnvelope` and its supporting types, including the flood subtype |
| `collections` | The persisted collection documents, the vector-index and TTL configs, and the catalog substrate |
| `catalog` | `CatalogEntry` - one vetted public data source in the curated catalog |
| `source_spec` | `SourceSpec` - the declarative data-router source specification a `source.yaml` validates against |
| `tool_registry` | `AtomicToolMetadata`, the TTL classes, the retrieval tiers, and the declared resolution ranges |
| `gate_spec` | The declarative confirm gate a tool carries, and the levers its card offers |
| `tool_metadata` | The required tool-docstring sections and the `tool_category` vocabulary |
| `execution` | `ModelSetup`, `ExecutionHandle`, `RunResult`, `LayerURI`, `LegendKey`, and the `LayerURI` result-model subclasses |
| `telemac_contracts` | The TELEMAC result layers and their declared style rows |
| `publish_manifest` | The typed reader for a worker's `publish_manifest.json` |
| `export_schemas` | Renders `contracts/schemas/` from the live models |

## Install

The package is installed editable as part of the repo's dev environment:

```bash
pip install -e contracts
```

Python `>= 3.11`, pydantic `>= 2, < 3`, `python-ulid >= 2, < 4`.

## Tests

```bash
make test-packages
```

Every message payload, every document and every result shape is exercised
through a real `JSON -> model -> JSON` round trip with idempotence checks,
alongside negative controls and the schema drift gate.

## Regenerate the JSON Schemas

```bash
./venvs/agent/bin/python -m trid3nt_contracts.export_schemas
```

Output is key-sorted and newline-terminated, so an unchanged contract set
re-renders byte-identically and `git diff` is the drift signal. A docstring or
`Field(description=...)` change therefore has to be committed together with the
regenerated `contracts/schemas/`.

## Wire form

`model.model_dump(mode="json")` is the canonical wire form. A document that
aliases `_id` dumps with `model.model_dump(**MONGO_DUMP_KWARGS)`, which adds
`by_alias=True`.

Datetimes serialize to ISO-8601 with a `Z` suffix. ULIDs are 26-character
strings. A `bbox` is always `[minLon, minLat, maxLon, maxLat]` in EPSG:4326. A
`payload` is always an object, `{}` when empty.

## Versioning

Each top-level document carries a `schema_version: Literal["v1"]` as its first
field. Growth is ADDITIVE - a new optional field, a new member of an open enum -
and a breaking change bumps the version. The enums most likely to grow
(`hazard_type`, `tool_category`, the forcing types) are open by design, so a new
engine registers a member without breaking a receiver.
