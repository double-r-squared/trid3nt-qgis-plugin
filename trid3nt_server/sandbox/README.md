# `sandbox/` - the code-exec box

Where a user-confirmed snippet runs: a staged working directory in, one
container run, a result envelope out. Everything the snippet may read is staged
before the container starts, so the box needs no network and no credentials of
its own.

## Files

| file | what it is |
| --- | --- |
| `__init__.py` | The box, as the server imports it. |
| `box.py` | Staging, the one container run, and reading the results back. |
| `driver.py` | What runs INSIDE the box; it imports nothing from the server package. |
