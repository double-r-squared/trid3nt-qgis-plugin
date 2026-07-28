# 0028 - dual-socket session-scoped registries

Context: the client mounts TWO WebSocket sockets per browser tab (one for
`user-message`, one for `case-command`), both carrying the same `session_id`.
Per-connection state on the SessionState dataclass would split-brain between the
two sockets, and a gate/picker opened on one socket must be resolvable by a reply
that lands on the other.

Decision: mutable session state that both sockets must agree on lives in
module-level registries keyed by `session_id` (or by an unguessable ULID
`request_id` tagged with its owning `session_id`), NOT on the per-connection
dataclass.
- `active_case_id` is a property backed by `_SESSION_ACTIVE_CASE[session_id]` so
  both sockets observe one active Case; writes go through the shared setter.
- `_SESSION_ANON_ID[session_id]` mirrors the active-Case registry for the
  anonymous-identity race: in the first-connect window before a client-owned
  `anonymous_user_id` is persisted, the two sockets must not each mint a
  different anon ULID and fork the owner-scoped case list.
- `_PENDING_CONFIRMATIONS` (keyed by `warning_id`) and `_PENDING_TOOL_CHOICES`
  (keyed by `request_id`) are module-level for the same reason: a confirm/pick
  opened on one socket resolves when the click arrives on the sibling socket.
- A per-connection `did_first_resume` latch is still needed because BOTH sockets
  independently send 25s keepalive resumes stamped with their own view of the
  active Case; without it the shared pointer would ping-pong every 25s.
- `_PERSISTENCE` is a deliberate module-level singleton (not per-connection): the
  Atlas MCP client is expensive to start (subprocess spawn / TLS), per-session
  writes need only a typed wrapper not connection isolation, and it resets on
  process restart so tests can swap it.

Consequence: consistent with 0002/0020 (one single user, remote access is the
same user) - these registries are single-user in-memory state, not a multi-user
store. A future multi-user split would replace them with per-user isolation.
