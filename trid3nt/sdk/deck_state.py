"""Portable reviewed state. References are to engine declarations and typed SDK data."""

from __future__ import annotations

import builtins
import dataclasses
import importlib
import math
import operator
import types
from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from functools import reduce
from pathlib import Path
from typing import Any

import numpy as np
from pydantic import BaseModel

from trid3nt.errors import DeclarationError
from trid3nt.inputs.slots import Slot
from trid3nt.inputs.temporal import Series
from trid3nt.workflows.runtime.module import UNSET

_TYPES = {cls.__name__: cls for cls in (str, int, float, bool, dict, list, tuple, object, type(None))}
_DECLARATIONS = ("trid3nt.inputs.", "trid3nt.workflows.", "trid3nt.tools.mesh.", "trid3nt_contracts.")


def _type_name(cls: type) -> str:
    module, name = cls.__module__, cls.__qualname__
    if module == builtins.__name__ and name in _TYPES:
        return name
    if module.startswith(_DECLARATIONS) and "." not in name:
        return f"{module}:{name}"
    raise DeclarationError("Unsupported reviewed state type.", error_code="REMOTE_STATE_UNSUPPORTED")


def _type(name: str) -> type:
    module, _, cls = name.partition(":")
    if not cls:
        return _TYPES[name]
    if not module.startswith(_DECLARATIONS) or not cls.isidentifier():
        raise ValueError("Invalid reviewed declaration.")
    result = getattr(importlib.import_module(module), cls)
    if not isinstance(result, type) or result.__module__ != module:
        raise ValueError("Invalid reviewed declaration.")
    return result


def encode(value: Any, workflow: Any, path, *, uri=lambda value: value) -> Any:
    """Write reviewed state as JSON values, assigning references to shared objects."""
    nodes = []
    seen = {}

    def visit(value):
        if type(value) is float and not math.isfinite(value):
            return ["float", value.hex()]
        if type(value) in (type(None), bool, int, float, str):
            return uri(value) if isinstance(value, str) and value.startswith("s3://") else value
        if value is workflow:
            return ["workflow"]
        if value is UNSET:
            return ["unset"]
        if isinstance(value, Slot):
            return ["input-slot", value.name]
        if isinstance(value, type) and hasattr(value, "MODULE_INPUT"):
            return ["steering", value.MODULE]
        if isinstance(value, type):
            return ["type", _type_name(value)]
        if isinstance(value, types.UnionType):
            return ["union", *map(visit, value.__args__)]
        if isinstance(value, types.GenericAlias):
            return ["generic", visit(value.__origin__), *map(visit, value.__args__)]
        if isinstance(value, Enum):
            return ["enum", _type_name(type(value)), visit(value.value)]
        if isinstance(value, Path):
            return ["path", path(value)]
        if isinstance(value, (date, datetime)):
            return [type(value).__name__, value.isoformat()]
        if isinstance(value, timedelta):
            return ["delta", value.days, value.seconds, value.microseconds]
        if isinstance(value, timezone):
            return ["zone", visit(value.utcoffset(None))]
        if isinstance(value, np.generic):
            return ["numpy-scalar", str(value.dtype), visit(value.item())]
        identity = id(value)
        if identity in seen:
            return ["ref", seen[identity]]
        index = len(nodes)
        seen[identity] = index
        nodes.append(None)
        if isinstance(value, Series):
            row = ["series", list(value.times_s), list(value.values), value.units]
        elif isinstance(value, np.ndarray) and not value.dtype.hasobject:
            row = ["array", str(value.dtype), list(value.shape), value.tobytes().hex()]
        elif isinstance(value, Mapping):
            runner_directory = value.get("rundir") if value.get("manifest_uri") else None
            row = ["readonly" if type(value) is types.MappingProxyType else "mapping",
                   [[visit(key), visit(Path(runner_directory) if key == "rundir" and runner_directory else item)]
                    for key, item in value.items()]]
        elif isinstance(value, (list, tuple, set, frozenset)):
            row = [type(value).__name__, [visit(item) for item in value]]
        elif dataclasses.is_dataclass(value):
            row = ["data", _type_name(type(value)), {f.name: visit(getattr(value, f.name))
                                                    for f in dataclasses.fields(value)}]
        elif isinstance(value, BaseModel):
            row = ["model", _type_name(type(value)), {key: visit(item) for key, item in value.__dict__.items()}]
        else:
            raise DeclarationError(f"Unsupported reviewed value type {type(value).__name__}.",
                                   error_code="REMOTE_STATE_UNSUPPORTED")
        nodes[index] = row
        return ["ref", index]

    return {"root": visit(value), "nodes": nodes}


def decode(document: dict, workflow: Any, path, *, uri=lambda value: value) -> Any:
    """Read a portable graph against the runner's template and local paths."""
    from trid3nt.inputs import slots

    by_name = {s.name: s for s in vars(slots).values() if isinstance(s, Slot)}
    refs = {}
    nodes = document["nodes"]

    def visit(token):
        if not isinstance(token, list):
            return uri(token) if isinstance(token, str) and token.startswith("s3://") else token
        tag, *values = token
        if tag == "float":
            return float.fromhex(values[0])
        if tag == "ref":
            return node(values[0])
        if tag == "workflow":
            return workflow
        if tag == "unset":
            return UNSET
        if tag == "input-slot":
            return by_name[values[0]]
        if tag == "steering":
            body = workflow.steering
            if body.MODULE != values[0]:
                from trid3nt.workflows.geoclaw.modules import body_on

                return body_on(body, values[0])
            return body
        if tag == "type":
            return _type(values[0])
        if tag == "union":
            return reduce(operator.or_, map(visit, values))
        if tag == "generic":
            origin, *args = map(visit, values)
            return types.GenericAlias(origin, tuple(args))
        if tag == "enum":
            return _type(values[0])(visit(values[1]))
        if tag == "path":
            return path(values[0])
        if tag == "date":
            return date.fromisoformat(values[0])
        if tag == "datetime":
            return datetime.fromisoformat(values[0])
        if tag == "delta":
            return timedelta(days=values[0], seconds=values[1], microseconds=values[2])
        if tag == "zone":
            return timezone(visit(values[0]))
        if tag == "numpy-scalar":
            return numeric_dtype(values[0]).type(visit(values[1]))
        raise ValueError("Invalid reviewed state token.")

    def numeric_dtype(value):
        dtype = np.dtype(value)
        if dtype.hasobject:
            raise ValueError("Invalid reviewed array type.")
        return dtype

    def node(index):
        if index in refs:
            return refs[index]
        tag, *parts = nodes[index]
        if tag in {"data", "model"}:
            name, fields = parts
            cls = _type(name)
            if tag == "data" and dataclasses.is_dataclass(cls):
                expected = {field.name for field in dataclasses.fields(cls)}
                if set(fields) != expected:
                    raise ValueError("Invalid reviewed state fields.")
                result = object.__new__(cls)
                refs[index] = result
                for key, value in fields.items():
                    object.__setattr__(result, key, visit(value))
            elif tag == "model" and issubclass(cls, BaseModel):
                result = cls.model_validate({key: visit(value) for key, value in fields.items()})
            else:
                raise ValueError("Invalid reviewed state class.")
        elif tag in {"mapping", "readonly"}:
            mapping = {}
            result = types.MappingProxyType(mapping) if tag == "readonly" else mapping
            refs[index] = result
            mapping.update((visit(key), visit(value)) for key, value in parts[0])
        elif tag == "list":
            refs[index] = result = []
            result.extend(map(visit, parts[0]))
        elif tag in {"tuple", "set", "frozenset"}:
            result = {"tuple": tuple, "set": set, "frozenset": frozenset}[tag](map(visit, parts[0]))
        elif tag == "series":
            result = Series(parts[0], parts[1], units=parts[2])
        elif tag == "array":
            dtype, shape, hex_ = parts
            result = np.frombuffer(bytes.fromhex(hex_), dtype=numeric_dtype(dtype)).reshape(shape).copy()
        else:
            raise ValueError("Invalid reviewed state node.")
        refs[index] = result
        return result

    return visit(document["root"])
