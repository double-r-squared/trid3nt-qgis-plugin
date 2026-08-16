"""Agent service: the WebSocket LLM-workbench daemon.

- WebSocket core (session-resume, user-message, cancel, error).
- LLM round-trip via provider dispatch (bedrock_adapter default, openai_adapter
  for OpenRouter and local models, scripted_adapter for tests) with streamed
  agent-message-chunk deltas. ``google.genai.types`` is the shared Content/Part
  containment layer the Bedrock/OpenAI adapters reuse.
- File-backed persistence (``FileMCPClient`` behind the ``MCPClientProtocol``
  seam; another document-store client drops in unchanged); all wire
  serialization through trid3nt_contracts.
"""

from __future__ import annotations

__version__ = "0.1.0"
