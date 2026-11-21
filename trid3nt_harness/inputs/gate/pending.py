"""The gate's owner-checked pending replies: one registry per kind of card a turn parks on.

Each is a ``PendingReplies``, keyed by an unguessable id the card carries, so a reply from
a sibling connection of the owning session resolves it and a reply from any other is refused.
"""
from __future__ import annotations

from trid3nt.session.pending import PendingReplies

__all__ = [
    "_PENDING_CONFIRMATIONS",
    "_PENDING_SPATIAL_INPUTS",
    "_PENDING_TOOL_CHOICES",
]


#: warning_id / code_exec_id of a confirmation card.
_PENDING_CONFIRMATIONS = PendingReplies("tool-payload-confirmation")
#: request_id of a draw surface; a draw with no reply resolves to None on timeout.
_PENDING_SPATIAL_INPUTS = PendingReplies("spatial-input-response")
#: request_id of a tool-candidates card.
_PENDING_TOOL_CHOICES = PendingReplies("tool-choice")
