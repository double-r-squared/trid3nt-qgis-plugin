# `credentials/` - who is connected, and what keys they hold

A local daemon has no identity provider: every connection resolves to the one
fixed local user, and the only real work here is holding a provider key for the
lifetime of a session without ever letting the key material onto the wire.

## Files

| file | what it is |
| --- | --- |
| `__init__.py` | The handshake, the registry and the resolver, as one surface. |
| `auth_handshake.py` | The WS connect handshake and the single local identity it resolves to. |
| `credential_registry.py` | Per-provider metadata a credential card needs - label, signup url, the env var - and no key material. |
| `resolver.py` | The runtime resolver: the in-memory session cache first, the environment behind it. |
