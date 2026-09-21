# `credentials/` - who is connected, and what keys they hold

A local daemon has no identity provider: every connection resolves to the one
fixed local user, and the only real work here is holding a provider key for the
lifetime of a session without ever letting the key material onto the wire. Which
key a source needs is the SOURCE's own statement, on its row.

## Files

| file | what it is |
| --- | --- |
| `__init__.py` | The handshake and the resolver, as one surface. |
| `auth_handshake.py` | The access token the daemon mints, the gate that verifies it, and the one session identity it binds. |
| `resolver.py` | The runtime resolver over the credential each source row declares: the in-memory session cache first, the row's env var behind it, and the refusal when neither holds one. |
