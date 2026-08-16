# persistence/ -- the document-store seam + case lifecycle

`trid3nt_server/persistence/` (ADR 0277 grouped the two former top-level
modules `persistence.py` + `case_lifecycle.py` into a package) is the storage
layer behind the `MCPClientProtocol` seam.

## What lives here

- `persistence.py` -- the store surface: `Persistence`, `MCPClientProtocol`,
  `FileMCPClient` (file-backed default), `make_file_persistence`,
  `make_persistence_for_backend`, collection names (`CASES_COLLECTION`,
  `SESSIONS_COLLECTION`, `CHAT_COLLECTION`, `USERS_COLLECTION`),
  `DEFAULT_DATABASE`, and dev-persistence env knobs. Another document-store
  client drops in unchanged behind the protocol.
- `case_lifecycle.py` -- builds the per-case QGS project on top of the store
  (`ensure_case_qgs`, `CaseLifecycleError`).
- `__init__.py` -- re-exports the store surface so `trid3nt_server.persistence.X`
  resolves unchanged after the module-to-package grouping.

## Composition

`case_lifecycle` imports `persistence` (one direction, no cycle).
`server/_core` and `server/session` read/write cases, chat turns, and sessions
through the store surface; `server/_core` imports
`persistence.case_lifecycle.ensure_case_qgs`.

## Invariants / extension points

- All wire serialization goes through `trid3nt_contracts`.
- The store is a protocol seam -- swapping file->document-store is a client
  swap, not a call-site change.
