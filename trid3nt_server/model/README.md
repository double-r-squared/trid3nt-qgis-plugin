# `model/` - the model connection

Everything between the turn loop and the model answering it: the provider
adapters that speak each provider's wire from the shared IR, the credentials a
connection holds, and the guards that bound what one model turn may do.

## Subfolders

| subfolder | what lives there |
| --- | --- |
| `adapters/` | The LLM provider adapters, behind one shared IR. |
| `credentials/` | The connect handshake, and the resolver over the credential each source row declares. |
| `guards/` | The guards over a model turn - the circuit breaker, the runaway guard, the context budget, tool gating and the error classifier they share. |
