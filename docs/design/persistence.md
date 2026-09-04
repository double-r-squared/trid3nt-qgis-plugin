# persistence.py -- the document-store seam

`trid3nt_server/persistence.py` is the storage layer behind the
`MCPClientProtocol` seam. One flat module: the seam has one implementation, so
a package directory around a single file bought a namespace and nothing else.

## What lives here

The store surface: `Persistence`, `MCPClientProtocol`, `FileMCPClient` (the
file-backed store, and the only one), `make_file_persistence`,
`make_persistence_for_backend`, collection names (`CASES_COLLECTION`,
`SESSIONS_COLLECTION`, `CHAT_COLLECTION`, `USERS_COLLECTION`),
`DEFAULT_DATABASE`, and the dev-persistence env knobs.

`CaseSummary.qgs_project_uri` stays as INERT DATA: a case that was handed an
explicit project URI keeps it, and nothing provisions one. The per-case `.qgs`
lazy-init that used to live here never had a production caller.

## Composition

`server/_core` and `server/session` read/write cases, chat turns, and sessions
through the store surface, reaching it through the
`server/session/persistence_ref` accessor rather than by constructing one.

## Invariants / extension points

- All wire serialization goes through `trid3nt_contracts`.
- The store is a protocol seam: `Persistence` is written against
  `MCPClientProtocol`, never against the files, so a second implementation is a
  client swap rather than a call-site change.
- `DEFAULT_DATABASE` is a constant. Test isolation relocates the whole root via
  `TRID3NT_DEV_PERSISTENCE_DIR` rather than renaming one namespace inside it.
