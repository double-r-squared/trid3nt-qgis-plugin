# `store/` - what the daemon keeps

The object store every run reaches through, the documents a case is kept as, and
the sweep that clears the cache bucket of what no live case pins.

## Files

| file | what it is |
| --- | --- |
| `__init__.py` | The package surface. |
| `objects.py` | The object store every run reaches through: the bound S3 client, or a case directory bound for one task (`FileStore`, the same calls over `<case>/<bucket>/<key>`, a `data/` read checked against its sha256); the two readings of the runs bucket - the one a read of a past run falls back on, and the one an upload refuses to default - the cache bucket and the work directory; the size of an object, with a missing one refused typed. |
| `journal.py` | The run journal on disk: where it lives (a bound case directory's `runs/`, else the persistence directory no sweep touches) and every record read back off it, a run's published layers among them. |
| `persistence_ref.py` | The process-global `Persistence` handle the daemon binds at start-up; unbound it is `None` and every caller handles that. |
| `cases.py` | The typed wrapper over the document store: cases, layers, chat, run snapshots. |
| `sweep.py` | What the cache bucket keeps: the uris a live case's runs pin, and the sweep that deletes every unpinned object past its TTL class's window. |
