# `trid3nt_contracts/` - the types that cross a boundary

One definition per shape, imported everywhere it is used, so the plugin, the
daemon and the workers cannot disagree about what a message is. Every model
subclasses `GraceModel`; ids are ULIDs. A shape here is the contract - a
consumer branches on its discriminator rather than on a string it recognised.

## Files

| file | what it is |
| --- | --- |
| `__init__.py` | The package door: every contract, one import away. |
| `auth.py` | The two envelopes of the connect handshake. |
| `case.py` | The Case persistence envelopes; `case_id` IS the project id. |
| `catalog.py` | `CatalogEntry` - one vetted public data source. |
| `chart_contracts.py` | The chart-emission envelope and its Vega-Lite wire format. |
| `collections.py` | The document-store collection schemas. |
| `common.py` | The primitives every other contract builds on: ids, timestamps, the base model. |
| `envelope.py` | `AssessmentEnvelope` - one output shape across memory, wire and storage. |
| `errors.py` | Typed errors as a closed code discriminator. |
| `execution.py` | The solver-execution shapes: setup, handle, result, and `LayerURI`. |
| `export_schemas.py` | Render every contract's JSON Schema into `contracts/schemas`, idempotently. |
| `gate_spec.py` | The declarative confirm-gate metadata a gated tool carries. |
| `outputs_manifest.py` | The `outputs.json` emit-on-solve manifest, writer and typed reader. |
| `payload_warning.py` | The payload-warning envelope and its confirmation. |
| `publish_manifest.py` | The typed reader for the worker's `publish_manifest.json`. |
| `py.typed` | The marker that says these annotations are shipped. |
| `region_choice.py` | The region-narrowing picker: the request that pauses a turn, and its reply. |
| `sandbox_contracts.py` | The two code-exec envelopes: confirm request and run result. |
| `secrets.py` | The per-Case secret envelopes; `secret-add` is the only one that carries key material. |
| `source_spec.py` | `SourceSpec` - the generic data-router source specification, as data. |
| `telemac_contracts.py` | One `LayerURI` subclass per TELEMAC product, each carrying the scalars a narration cites. |
| `tool_metadata.py` | The tool docstring sections and the `tool_category` vocabulary. |
| `tool_registry.py` | `AtomicToolMetadata` - what every registered tool declares. |
| `user.py` | The User account record. |
| `ws.py` | The WebSocket protocol: the shared envelope and every message payload. |
