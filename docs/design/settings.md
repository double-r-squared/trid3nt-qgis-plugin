# settings -- one home for the judgments a person may change

`trid3nt_server/settings/` carries every judgment the system makes that a person
could reasonably make differently: how far a hole is filled, how large a merge
may be, how long a card waits. A constant of that kind is never written in the
module that uses it; it is an entry in one dictionary, and the module calls
`setting("NAME")`.

## The dictionary

`settings/module_input/settings.json` is a keyword dictionary in the engines' own
format, read by `load_module_input`. A row's rubrique is its family (`bed`,
`notes`, `gate`, `render`, `solve`, `server`, `model`), its help says what it decides, its default is the cited
default, and its `source` quotes where the default comes from; a default with no
published source says so (`TRID3NT engineering default.`). The range a value is
refused outside, and its unit, sit in `settings/module_bounds.json`, the same
sidecar the engines use. `describe_keywords(module="settings", query=...)`
discovers entries; nothing else reads the dictionary for discovery.

A value may be a list of fixed length (`CLIP_PERCENTILES` is a low and a high
percentile); its range holds every item, and the dock takes it comma-separated.

A name is spelled once. A judgment two modules used to hold separately (the
gate card's time-to-live and rounds, the bed interpolation, the observation
window) is one entry both read.

## Three levels

A value is the run's if the run states it, else the user's file, else the
dictionary default:

| level | where it lives | how it is stated |
|---|---|---|
| default | `settings.json` in the package | - |
| user | `<TRID3NT_HOME or ~/.trid3nt>/settings.json`, a mapping of names to values | `set_config(scope="user")`, or Settings in the dock |
| run | the run | `keywords={"settings: FILL_MAX_DISTANCE": 200}` on a template call, or `set_config(scope="run")`, which binds the session's next run |

`settings.setting(name)` is the one reader. It journals every read on the run
in progress with the level it resolved at; the run's journal line carries them
as `settings: [{name, value, level}]`, beside its outputs. A `settings:` keyword
is taken out of the keyword floor at the fill and held to its entry; a value
outside the range is a refused keyword, never a run.

## Changing one

`set_config(name, value, scope, reset)` is always gated: a value outside the
entry's range refuses before any card opens, then the one input-review gate
presents a one-row card with the name, the value in force and its level, the new
value, the scope and what the entry decides. The person may edit the value on
the card (held to the same range); only a proceed writes. The dock's Settings
list reads `GET /api/settings`, never the file, and its Set and Reset run
`set_config` through the same `!run` seam, so they open the same card.

## What is not a setting

A value a source, an engine or a format fixes; a bound that is a fact of the
world; a wire constant; an internal limit a person has no reason to change (a
registry cap, a poll interval, a drain timeout, a heartbeat), which is one
constant in one module. Environment variables stay for deployment and secrets:
a variable that steers a setting is removed when the setting lands, and one that
steers an internal limit becomes the constant.
