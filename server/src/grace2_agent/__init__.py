"""GRACE-2 agent service (Bedrock / OpenAI-OpenRouter on EC2).

Live stack (post GCP->AWS migration; offline-pivot cleanup):
- Appendix-A WebSocket core (session-resume, user-message, cancel, error).
- LLM round-trip via provider dispatch (bedrock_adapter default, openai_adapter
  for OpenRouter, scripted_adapter for tests) with streamed agent-message-chunk
  deltas.  The dormant raw google-genai/Vertex Gemini generate path and the ADK
  seam are decommissioned; ``google.genai.types`` is retained ONLY as the shared
  Content/Part containment layer the live Bedrock/OpenAI adapters reuse.
- MongoDB/DynamoDB persistence; all wire serialization through grace2_contracts.
"""

from __future__ import annotations

__version__ = "0.1.0"
