# persistence/ -- the document-store seam

`trid3nt_server/persistence/` is the storage layer behind the
`MCPClientProtocol` seam.

## What lives here

- `persistence.py` -- the store surface: `Persistence`, `MCPClientProtocol`,
  `FileMCPClient` (file-backed default), `make_file_persistence`,
  `make_persistence_for_backend`, collection names (`CASES_COLLECTION`,
  `SESSIONS_COLLECTION`, `CHAT_COLLECTION`, `USERS_COLLECTION`),
  `DEFAULT_DATABASE`, and dev-persistence env knobs. Another document-store
  client drops in unchanged behind the protocol.
- `__init__.py` -- re-exports the store surface so `trid3nt_server.persistence.X`
  resolves unchanged.

`CaseSummary.qgs_project_uri` stays as INERT DATA: a case that was handed an
explicit project URI keeps it, and nothing provisions one. The per-case `.qgs`
lazy-init that used to live here never had a production caller.

## Composition

`server/_core` and `server/session` read/write cases, chat turns, and sessions
through the store surface.

## Invariants / extension points

- All wire serialization goes through `trid3nt_contracts`.
- The store is a protocol seam -- swapping file->document-store is a client
  swap, not a call-site change.
