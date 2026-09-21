# `gates/` - where a turn stops

Every place the agent loop is allowed to pause, refuse or narrow. A gate either
blocks the turn on a card the user answers, or it bounds what the loop may do
before it runs away. Nothing here decides physics: a gate presents what was
resolved and carries back what the user said.

## Files

| file | what it is |
| --- | --- |
| `__init__.py` | The gate surface the turn engine imports. |
| `actionability.py` | The three-way classifier over a dispatch error: agent, user, operator. |
| `circuit_breaker.py` | The per-session breaker over consecutive UPSTREAM tool failures. |
| `confirm.py` | The confirm engine and the user-decision gates that park a turn on a card. |
| `context_budget.py` | Per-model window discovery and the client-side history management it drives. |
| `draw_input.py` | The DRAW gate: one declared param's value asked for on the canvas. |
| `fallback.py` | The loudness floor over the confirm spine; a synthetic rung always pauses. |
| `input_review.py` | The two-mode review gate - `auto` labels every non-user input, `user_gated` presents the sheet. |
| `pending.py` | The session-scoped registry every blocking gate registers into. |
| `runaway_guard.py` | Step cap, wall clock and loop watchdog, OR'd into one abort. |
| `spatial_input.py` | A drawn `FeatureCollection` adapted into engine inputs. |
| `spatial_input_tool.py` | The draw gate's tool face: `request_spatial_input`, the registered tool that raises the gate. |
| `spatial_roles.py` | The drawn-geometry role vocabulary and its parser - structural only. |
| `tool_gating.py` | Per-turn top-k tool gating, on the local provider path only. |

## Subfolders

| subfolder | what lives there |
| --- | --- |
| `cards/` | One builder per card the client renders: estimate, payload warning, solver confirm, spatial input. |
