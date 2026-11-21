"""A fetched RECORD read by its source's field map: ``rec.value``, ``rec.accuracy``, ``rec.raw["x"]``.

Every field the source returned stays on ``raw`` under its own name; a member is read
through the map the source states in its ``source.yaml``. A member the source does not
map, a mapped field empty on this record, and a code its table does not hold each refuse
by name - never a default.
"""

from __future__ import annotations

from typing import Any, Mapping, NamedTuple

from trid3nt_contracts.coverage import NUMERIC_MEMBERS, SERIES_MEMBERS, UnitField, Member, Rule

__all__ = ["FieldError", "Measured", "Record", "produced_by", "records"]


class FieldError(RuntimeError):
    """A typed refusal naming the source, the member and, where one is known, the record."""

    retryable: bool = False

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


class Measured(NamedTuple):
    """A numeric member as read: the number (a series stays the source's own text) and its unit."""

    value: Any
    unit: str | None


def produced_by(layer: Any) -> tuple[str, dict[str, Member]] | None:
    """The source a fetched layer came from and its map, by the handle or uri the layer is registered
    under; ``None`` for a layer no registered source produced."""
    from trid3nt.render.uri_registry import producer_of_handle
    from trid3nt.tools.fetchers._router.registration import get_spec

    uri = layer if isinstance(layer, str) else (
        layer.get("uri") if isinstance(layer, Mapping) else getattr(layer, "uri", None))
    fetcher = producer_of_handle(str(uri)) if uri else None
    spec = get_spec(fetcher) if fetcher else None
    return (fetcher, dict(spec.fields)) if spec is not None else None


class Record:
    """One record of a source: its raw fields untouched, its members read through the map."""

    __slots__ = ("raw", "fields", "source", "site")

    def __init__(self, raw: Mapping[str, Any], fields: Mapping[str, Member], *,
                 source: str, site: str | None = None) -> None:
        self.raw = raw
        self.fields = fields
        self.source = source
        self.site = site

    def __repr__(self) -> str:
        return f"Record({self.source}, {self.site or 'unnamed'})"

    def maps(self, name: str) -> bool:
        """Whether the source maps this member at all."""
        return name in self.fields

    def field(self, name: str) -> str:
        """The record's own field a member names, "" where the member is no field."""
        return self._member(name).field

    def stated(self, name: str) -> Any:
        """A field member's raw value on this record, ``None`` where it is unmapped or empty."""
        member = self.fields.get(name)
        if member is None or not member.field:
            return None
        found = self._raw(member.field)
        return None if _empty(found) else found

    def unit(self, name: str) -> str | None:
        """The unit a member is counted in on this record, ``None`` where none is stated."""
        member = self.fields.get(name)
        if member is None:
            return None
        if isinstance(member.unit, UnitField):
            found = self._raw(member.unit.field)
            return None if _empty(found) else str(found).strip()
        return member.unit or None

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return self.read(name)

    def read(self, name: str) -> Any:
        """A member's value: numeric members as :class:`Measured`, ``time`` as the source wrote it."""
        member = self._member(name)
        if member.constant is not None:
            return Measured(float(member.constant), str(member.unit))
        if member.rule is not None:
            return self._ruled(name, member.rule)
        stated = self._raw(member.field)
        if _empty(stated):
            raise self._refusal("FIELD_EMPTY", name,
                                f"maps {name!r} to its field {member.field!r}, which is empty on this record")
        if member.table is not None:
            found = member.table.get(str(stated).strip())
            if found is None:
                raise self._refusal("FIELD_UNTABLED", name,
                                    f"reads {name!r} off {member.field!r} through a table that holds "
                                    f"no row for the code this record carries ({str(stated)[:60]!r})")
            if isinstance(found, Rule):
                return self._ruled(name, found)
            stated = found
        if name not in NUMERIC_MEMBERS:
            return stated
        unit = self._unit(name, member)
        if name in SERIES_MEMBERS:
            return Measured(stated, unit)
        try:
            return Measured(float(stated), unit)
        except (TypeError, ValueError):
            raise self._refusal("FIELD_NOT_A_NUMBER", name,
                                f"maps {name!r} to {member.field!r}, which holds "
                                f"{str(stated)[:60]!r} on this record, not a number") from None

    def cite(self, name: str) -> str:
        """Where a mapped member's number is read from: the publication it cites, or the source's own field."""
        member = self._member(name)
        return member.cite or f"{self.source} field {member.field}"

    def cited(self, name: str) -> list[Measured]:
        """Every number a member's table cites, each rule read on this record; none for a member with no table."""
        member = self.fields.get(name)
        rows = () if member is None or member.table is None else member.table.values()
        return [self._ruled(name, row) if isinstance(row, Rule) else Measured(float(row), self._unit(name, member))
                for row in rows if isinstance(row, (Rule, int, float))]

    def _member(self, name: str) -> Member:
        member = self.fields.get(name)
        if member is None:
            raise self._refusal("FIELD_UNMAPPED", name,
                                f"maps no {name!r}: it does not report one, so nothing reads it "
                                "as if it did")
        return member

    def _raw(self, field: str) -> Any:
        # A flattened vector record carries a nested field under its dotted name; a record layer nests it.
        if field in self.raw:
            return self.raw[field]
        from trid3nt.tools.fetchers._router.field_map import read_path

        return read_path(dict(self.raw), field)

    def _unit(self, name: str, member: Member) -> str | None:
        if isinstance(member.unit, UnitField):
            stated = self._raw(member.unit.field)
            if _empty(stated):
                raise self._refusal("FIELD_EMPTY", name,
                                    f"states the unit of {name!r} in its field "
                                    f"{member.unit.field!r}, which is empty on this record")
            return str(stated).strip()
        return member.unit or None

    def _ruled(self, name: str, rule: Rule) -> Measured:
        """A published rule: the larger of its absolute bound and its fraction of this record's value."""
        if not rule.relative:
            return Measured(float(rule.absolute), rule.unit)
        from .temporal import TemporalUnitsError, convert_units

        reading = self.read("value")
        try:
            magnitude = abs(convert_units(float(reading.value), str(reading.unit), rule.unit))
        except (TypeError, ValueError, TemporalUnitsError) as exc:
            raise self._refusal("FIELD_RULE_UNREADABLE", name,
                                f"states {name!r} as a fraction of the value, and the value "
                                f"{reading.value!r} {reading.unit} is not a number in {rule.unit} "
                                f"({exc})") from exc
        return Measured(max(float(rule.absolute), float(rule.relative) * magnitude), rule.unit)

    def _refusal(self, code: str, name: str, what: str) -> FieldError:
        at = f" at {self.site}" if self.site else ""
        return FieldError(code, f"{self.source}{at} {what}. Member {name!r}; state the value on "
                                "the call, or name a source that reports it.")


def records(layer: Any, fields: Mapping[str, Member], *, source: str,
            site_fields: tuple[str, ...] = ()) -> list[Record]:
    """Every record of a fetched layer (a feature each; a record layer is one), read by ``fields``.

    ``site_fields`` names the raw fields a refusal says the record by, the first one present."""
    from .geometry import read_geometry_doc

    doc = read_geometry_doc(layer)
    if str(doc.get("type") or "") == "FeatureCollection":
        raws = [dict(f.get("properties") or {}) for f in doc.get("features") or ()
                if isinstance(f, Mapping)]
    elif doc.get("type") == "Feature":
        raws = [dict(doc.get("properties") or {})]
    else:
        raws = [dict(doc)]
    return [Record(raw, fields, source=source,
                   site=next((str(raw[k]) for k in site_fields if not _empty(raw.get(k))), None))
            for raw in raws]


def _empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and value != value:
        return True
    return isinstance(value, str) and not value.strip()

