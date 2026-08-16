"""Shared Qt stylesheet constants used by >=2 UI modules (cards + dock + cases dialog).

Split out of dock.py (2026-07-21 flat->package restructure). Value-identical move.
"""


_STATUS_LINE_STYLE = "color: palette(mid); font-size: 8pt; padding-left: 4px;"


# Style constants for the thinking block (F9, live-feedback 2026-07-09).
_THINKING_TOGGLE_STYLE = "color: palette(mid); font-size: 8pt; border: none; text-align: left;"
_THINKING_BLOCK_STYLE = (
    "background-color: palette(window); border-left: 2px solid palette(mid); "
    "border-radius: 2px; padding: 4px 6px; font-size: 8pt; color: palette(mid);"
)
# Probe-panel error variant (BUG 3b, live-feedback 2026-07-12): the same
# block chrome as the thinking body but in the error red, so a failed probe
# is unmistakable without landing in chat.
_PROBE_ERROR_BLOCK_STYLE = (
    "background-color: palette(window); border-left: 2px solid #f85149; "
    "border-radius: 2px; padding: 4px 6px; font-size: 8pt; color: #f85149;"
)
