"""GRACE-2 agent service (ADK + Gemini on Cloud Run).

job-0015 hello-world scope:
- Appendix-A WebSocket core (session-resume, user-message, cancel, error).
- Gemini round-trip with streamed agent-message-chunk deltas.
- MongoDB MCP sidecar over stdio (SRV from Secret Manager + ADC).
- All wire serialization through grace2_contracts (no hand-rolled JSON).
"""

from __future__ import annotations

__version__ = "0.1.0"
