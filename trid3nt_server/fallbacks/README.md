# `fallbacks/` - declared degradation

A capability that can degrade declares the ordered rungs it may descend, as
data. One walker executes every ladder, and a run's activations are persisted
beside its results, so "which rung answered" is a fact a reader can check rather
than a thing the narration claims.

## Files

| file | what it is |
| --- | --- |
| `__init__.py` | The ladder registry and the walker, as one surface. |
| `ladder.py` | The rung schema and the registry: a ladder is data, not code. |
| `persist.py` | One `fallback_activations.json` per run, beside the run's own outputs. |
| `walker.py` | The one walker: rungs in order, and only a degradation rung is gated. |
