"""genai Schema -> JSON Schema conversion for the provider wire formats.

``google.genai`` ``FunctionDeclaration`` is the internal tool IR; provider APIs
take plain JSON Schema.
"""

from __future__ import annotations

from typing import Any

# genai Schema ``type`` is an uppercase enum (STRING/OBJECT/...); JSON Schema is
# lowercase.
_TYPE_MAP = {
    "STRING": "string",
    "NUMBER": "number",
    "INTEGER": "integer",
    "BOOLEAN": "boolean",
    "ARRAY": "array",
    "OBJECT": "object",
    "TYPE_UNSPECIFIED": "string",
}


def genai_schema_to_json_schema(node: Any) -> dict[str, Any]:
    """Recursively convert a genai-dumped Schema dict to JSON Schema."""
    if not isinstance(node, dict):
        return {"type": "string"}
    out: dict[str, Any] = {}
    raw_type = node.get("type")
    if raw_type is not None:
        t = raw_type.value if hasattr(raw_type, "value") else str(raw_type)
        out["type"] = _TYPE_MAP.get(t.upper(), t.lower())
    if node.get("description"):
        out["description"] = node["description"]
    if node.get("enum"):
        out["enum"] = list(node["enum"])
    if node.get("format"):
        out["format"] = node["format"]
    props = node.get("properties")
    if isinstance(props, dict):
        out["properties"] = {
            k: genai_schema_to_json_schema(v) for k, v in props.items()
        }
    items = node.get("items")
    if items is not None:
        out["items"] = genai_schema_to_json_schema(items)
    if node.get("required"):
        out["required"] = list(node["required"])
    # An object schema must declare a properties map even when it is empty.
    if out.get("type") == "object" and "properties" not in out:
        out["properties"] = {}
    return out
