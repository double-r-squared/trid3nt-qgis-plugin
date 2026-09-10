"""Shared Qt stylesheet constants used by more than one UI module."""


_STATUS_LINE_STYLE = "color: palette(mid); font-size: 8pt; padding-left: 4px;"


_THINKING_TOGGLE_STYLE = "color: palette(mid); font-size: 8pt; border: none; text-align: left;"
_THINKING_BLOCK_STYLE = (
    "background-color: palette(window); border-left: 2px solid palette(mid); "
    "border-radius: 2px; padding: 4px 6px; font-size: 8pt; color: palette(mid);"
)
# The probe-panel error variant: the same block chrome as the thinking body
# but in the error red, so a failed probe is unmistakable without landing in
# chat.
_PROBE_ERROR_BLOCK_STYLE = (
    "background-color: palette(window); border-left: 2px solid #f85149; "
    "border-radius: 2px; padding: 4px 6px; font-size: 8pt; color: #f85149;"
)
