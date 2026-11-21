# `render/` - emission to the connected client

The daemon's half of render: the session's pipeline state. It binds the SDK's
emission hook (`trid3nt/render/emission.py`) around each call, and every layer
it tracks crosses the SDK's emission seam (`trid3nt/render/layer_uri_emit.py`).

## Files

| file | what it is |
| --- | --- |
| `__init__.py` | The package door. |
| `pipeline_emitter.py` | One session's pipeline snapshot and its accumulating `loaded_layers`. |
