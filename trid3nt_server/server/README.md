# `server/` - the daemon core

The connection loop, the turn engine, tool dispatch and session state. This
package holds what is true of the process rather than of any one question: which
sockets are open, which turn is running, what a tool call is allowed to do and
where its results go.

## Files

| file | what it is |
| --- | --- |
| `__init__.py` | The core's door; the subpackages below hold the working parts. |
| `config.py` | Environment-knob readers, each a pure `env -> value` read taken live. |
| `errors.py` | The typed dispatch error taxonomy - an `error_code` and a `retryable` flag per type. |
| `interactions.py` | The tool-choice and credential request/response gates. |
| `spatial.py` | Bbox and AOI helpers, and the region-choice and spatial pending-input registries. |

## Subfolders

| subfolder | what lives there |
| --- | --- |
| `dispatch/` | One tool call end to end: the AOI it runs over, the emitter it publishes through, how its results are summarized, persisted and reused. |
| `protocol/` | The wire: authentication, the connection registry, the message handlers, the HTTP catalog and the accept loop. |
| `session/` | What one connection holds - the case it is on, its persistence handle, its mutable state. |
| `turn/` | The turn engine: the model stream, the wire envelopes it produces, and the case bookkeeping around it. |
