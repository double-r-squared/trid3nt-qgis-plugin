# `guards/` - what bounds a model turn

A guard watches the model's own turn and stops or narrows it before it runs
away: too many steps, too long, too many upstream failures, a history past the
window, a tool surface too wide for a local model. None of them asks the user
anything; a pause on a person's input is the input gate's job.

## Files

| file | what it is |
| --- | --- |
| `__init__.py` | The package door. |
| `actionability.py` | The three-way classifier over a dispatch error: agent, user, operator. |
| `circuit_breaker.py` | The per-session breaker over consecutive UPSTREAM tool failures. |
| `context_budget.py` | Per-model window discovery and the client-side history management it drives. |
| `runaway_guard.py` | Step cap, wall clock and loop watchdog, OR'd into one abort. |
| `tool_gating.py` | Per-turn top-k tool gating, on the local provider path only. |
