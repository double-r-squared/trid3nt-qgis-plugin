# `gate/` - where a turn waits on a person

Every place the turn parks on a card a person replies to: the review of what was
resolved, a value drawn on the canvas, a confirm before a solve. Nothing here
decides physics: a gate presents what was resolved and carries back what the
user said.

## Files

| file | what it is |
| --- | --- |
| `__init__.py` | The gate surface the turn engine imports. |
| `confirm.py` | The confirm engine, the user-decision gates that park a turn on a card, and their decision windows. |
| `errors.py` | The typed gate refusals - a declined card, an expired card, an invalid draw reply - each with an `error_code` and a `retryable` flag. |
| `draw_input.py` | The DRAW gate: one declared param's value asked for on the canvas. |
| `input_review.py` | The two-mode review gate - `auto` labels every non-user input, `user_gated` presents the card. |
| `pending.py` | The owner-checked pending-reply registry: one per kind of reply (confirmation, draw, tool choice, processing, layer), each refusing a reply from another session. |
| `spatial_input_tool.py` | The draw gate's tool face: `request_spatial_input`, the registered tool that raises the gate. |
| `spatial_roles.py` | The drawn-geometry role vocabulary and its parser - structural only. |

## Subfolders

| subfolder | what lives there |
| --- | --- |
| `cards/` | One builder per card the client renders: estimate, payload warning, solver confirm, spatial input. |
