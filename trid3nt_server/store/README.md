# `store/` - what the daemon keeps

The object store every run reaches through, the documents a case is kept as, and
the sweep that clears the cache bucket of what no live case pins.

## Files

| file | what it is |
| --- | --- |
| `__init__.py` | The package door. |
| `objects.py` | The object store every run reaches through: the bound S3 client and the two readings of the runs bucket - the one a read of a past run falls back on, and the one an upload refuses to default - and the size of an object, with a missing one refused typed. |
| `cases.py` | The typed wrapper over the document store: cases, layers, chat, run snapshots. |
| `sweep.py` | What the cache bucket keeps: the uris a live case's runs pin, and the sweep that deletes every unpinned object past its TTL class's window. |
