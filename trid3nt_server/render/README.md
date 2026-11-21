# `render/` - emission to the connected client

The daemon's half of render: the session's pipeline state and the one seam every
client-bound `LayerURI` crosses before it is tracked. The format set itself is
the SDK's (`trid3nt/render/`).

## Files

| file | what it is |
| --- | --- |
| `__init__.py` | The package door. |
| `layer_uri_emit.py` | The single seam every client-bound `LayerURI` crosses. |
| `pipeline_emitter.py` | One session's pipeline snapshot and its accumulating `loaded_layers`. |
