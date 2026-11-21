# `settings/` - the changeable judgments

One keyword dictionary of every judgment a person may change, one reader, and the
user's file. Design: `docs/design/settings.md`.

## Files

| file | what it is |
| --- | --- |
| `__init__.py` | The surface callers import: `setting`, `resolve`, `split_keywords`, the run and session bindings. |
| `reader.py` | Resolution across default, the user file and the run; the user file's read and write; the journal of every read. |
| `module_bounds.json` | The unit and range each numeric entry is refused outside. |

## Subfolders

| subfolder | what lives there |
| --- | --- |
| `module_input/` | `settings.json`, the dictionary: family as rubrique, help, cited default and `source` per row. |
