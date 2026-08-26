"""Loader for the STYLE CONTRACT (``styles.yaml``). Data access only.

The contract is DATA, so it is read here - by anything that needs to know what a
preset declares. The RESOLVER (what turns a preset plus a raster into a scale a
renderer can use) lives once in ``trid3nt_server/emission/styles.py``; keeping the
loader in contracts is what lets a producer read its own legend classes without an
import pointing backwards from tools into emission.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from typing import Any, Literal, Mapping

__all__ = [
    "PresetSpec",
    "ScaleSpec",
    "STYLES_FILENAME",
    "default_scale",
    "preset",
    "preset_names",
    "presets",
    "quantity_defaults",
    "schema_version",
]

STYLES_FILENAME = "styles.yaml"

Policy = Literal["data", "fixed"]
Transform = Literal["linear", "log", "sqrt", "percentile"]
Kind = Literal["continuous", "categorical", "mesh"]


@dataclass(frozen=True, slots=True)
class ScaleSpec:
    """The scale vocabulary - ONE schema for all four entry points.

    The contract default, a template's ``.style()`` modifier, a declared param
    knob and ``restyle_layer``'s arguments all produce one of these, and a later
    stage overrides an earlier one field by field. ``range`` under
    ``policy="data"`` is the FALLBACK the resolver uses when the raster's own band
    statistics cannot be read - never a silent second opinion about the data.
    """

    policy: Policy = "data"
    range: tuple[float, float] | None = None
    transform: Transform = "linear"
    clip: tuple[float, float] | None = None

    def merged(self, other: "ScaleSpec | None") -> "ScaleSpec":
        """``other`` wins field by field - the override order, in one place."""
        if other is None:
            return self
        return ScaleSpec(
            policy=other.policy or self.policy,
            range=other.range if other.range is not None else self.range,
            transform=other.transform or self.transform,
            clip=other.clip if other.clip is not None else self.clip,
        )

    def as_doc(self) -> dict[str, Any]:
        return {"policy": self.policy, "range": list(self.range) if self.range else None,
                "transform": self.transform,
                "clip": list(self.clip) if self.clip else None}


@dataclass(frozen=True, slots=True)
class PresetSpec:
    """One declared style: what it IS, not how a particular renderer draws it."""

    name: str
    kind: Kind = "continuous"
    colormap: str = "viridis"
    scale: ScaleSpec = ScaleSpec()
    units: str | None = None
    label: str | None = None
    #: Explicit class breaks ``(lo, hi, hex_colour, label)`` for a categorical
    #: preset. The legend key and the paint are built from this ONE table.
    classes: tuple[tuple[float, float, str, str], ...] = ()


def _scale_from(doc: Any, fallback: ScaleSpec) -> ScaleSpec:
    if not isinstance(doc, Mapping):
        return fallback
    rng = doc.get("range")
    clip = doc.get("clip")
    return ScaleSpec(
        policy=str(doc.get("policy") or fallback.policy),
        range=(float(rng[0]), float(rng[1])) if rng else fallback.range,
        transform=str(doc.get("transform") or fallback.transform),
        clip=(float(clip[0]), float(clip[1])) if clip else fallback.clip,
    )


@lru_cache(maxsize=1)
def _document() -> dict[str, Any]:
    import yaml

    text = resources.files("trid3nt_contracts").joinpath(STYLES_FILENAME).read_text(
        encoding="utf-8")
    doc = yaml.safe_load(text)
    if not isinstance(doc, dict):
        raise ValueError(f"{STYLES_FILENAME} is not a mapping")
    return doc


def schema_version() -> int:
    return int(_document().get("schema_version") or 0)


@lru_cache(maxsize=1)
def default_scale() -> ScaleSpec:
    return _scale_from((_document().get("defaults") or {}).get("rescale"), ScaleSpec())


@lru_cache(maxsize=1)
def presets() -> Mapping[str, PresetSpec]:
    doc = _document()
    defaults = doc.get("defaults") or {}
    base = default_scale()
    out: dict[str, PresetSpec] = {}
    for name, spec in (doc.get("presets") or {}).items():
        spec = spec or {}
        out[name] = PresetSpec(
            name=name,
            kind=str(spec.get("kind") or defaults.get("kind") or "continuous"),
            colormap=str(spec.get("colormap") or defaults.get("colormap") or "viridis"),
            scale=_scale_from(spec.get("rescale"), base),
            units=spec.get("units"),
            label=spec.get("label"),
            classes=tuple((float(c[0]), float(c[1]), str(c[2]), str(c[3]))
                          for c in (spec.get("classes") or ())),
        )
    return out


def preset(name: str | None) -> PresetSpec | None:
    return presets().get((name or "").strip().lower())


def preset_names() -> frozenset[str]:
    return frozenset(presets())


@lru_cache(maxsize=1)
def quantity_defaults() -> Mapping[str, str]:
    """quantity -> preset name. The producer declares the quantity, never the style."""
    return dict((_document().get("quantity_defaults") or {}))
