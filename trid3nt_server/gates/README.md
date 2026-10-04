# `gates/` - where a turn stops

Every place the agent loop is allowed to pause, refuse or narrow. A gate either
blocks the turn on a card the user answers, or it bounds what the loop may do
before it runs away. Nothing here decides physics: a gate presents what was
resolved and carries back what the user said.

## Files

| file | what it is |
| --- | --- |
| `__init__.py` | The gate surface the turn engine imports. |
| `confirm.py` | The confirm engine and the user-decision gates that park a turn on a card. |
| `draw_input.py` | The DRAW gate: one declared param's value asked for on the canvas. |
| `input_review.py` | The two-mode review gate - `auto` labels every non-user input, `user_gated` presents the sheet. |
| `pending.py` | The session-scoped registry every blocking gate registers into. |
| `spatial_input.py` | A drawn `FeatureCollection` adapted into engine inputs. |
| `spatial_input_tool.py` | The draw gate's tool face: `request_spatial_input`, the registered tool that raises the gate. |
| `spatial_roles.py` | The drawn-geometry role vocabulary and its parser - structural only. |

## Subfolders

| subfolder | what lives there |
| --- | --- |
| `cards/` | One builder per card the client renders: estimate, payload warning, solver confirm, spatial input. |
