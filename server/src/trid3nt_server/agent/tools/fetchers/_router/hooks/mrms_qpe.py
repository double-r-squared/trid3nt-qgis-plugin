"""mrms_qpe hooks (grib_object + resolve phase, ADR 0069): NOAA MRMS MultiSensor
QPE gauge-corrected precipitation, a whole-object ``.grib2.gz`` behind an S3 key.

The MRMS key is discovered by an S3 list-objects walk -- ``latest`` (newest file in
the most-recent date directory) or a targeted ``valid_time`` (the nearest-earlier
published hour within a 24 h walkback). Both fit the SINGLE-round resolve phase
(ADR 0063/0064): ``resolve_build`` emits the candidate list-object probes (all return
HTTP 200 with-or-without the key, never a 404), the router GETs them, and
``resolve_parse`` scrapes the ``<Key>`` set + picks the resolved key, merging it into
``params`` PRE-cache-key. The main fetch is the ``grib_object`` raster access mode
(whole-object GET + gunzip + GRIB decode + window + sentinel-nodata). These hooks are
PURE: the router owns every round trip, the transport, the cache, and the COG serialize.
"""

from __future__ import annotations

import datetime as _dt
import re
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from .. import hooks as _hooks
from ..errors import router_input_error, router_not_available_error, router_upstream_error

__all__ = ["resolve_build", "resolve_parse"]

_QPE_PASS = "Pass2"
_KEY_RE = re.compile(r"<Key>([^<]+\.grib2\.gz)</Key>")

#: user-facing accumulation -> canonical S3 product token (the twin's alias map).
_ACCUM_ALIAS_MAP: dict[str, str] = {
    "1h": "01H", "3h": "03H", "6h": "06H", "12h": "12H",
    "24h": "24H", "48h": "48H", "72h": "72H",
    "01H": "01H", "03H": "03H", "06H": "06H", "12H": "12H",
    "24H": "24H", "48H": "48H", "72H": "72H",
}

#: number of trailing date directories the latest walk lists (Pass2 lags ~2 h, so
#: the newest file is within today or yesterday UTC); the twin lists ALL date
#: prefixes then the newest date's files -- listing the last two date dirs + picking
#: the max key resolves the identical latest file in the normal case (ADR 0069 (a)).
_LATEST_DATE_DIRS = 2

#: targeted-mode walkback ceiling (hours), the twin's 24 h nearest-earlier probe.
_WALKBACK_HOURS = 24


def _normalize_accumulation(spec: SourceSpec, accumulation: Any) -> str:
    """Canonicalize a user accumulation ('24h'/'24H'/...) or raise (twin parity)."""
    canonical = _ACCUM_ALIAS_MAP.get(str(accumulation))
    if canonical is None:
        raise router_input_error(
            spec.error_code_prefix,
            f"unknown accumulation={accumulation!r}; accepted values: 1h, 6h, 24h, 72h "
            f"(and 3h, 12h, 48h); uppercase aliases 01H, 06H, 24H, 72H are also accepted",
            spec.input_error_suffix)
    return canonical


def _parse_valid_time(spec: SourceSpec, valid_time: Any) -> _dt.datetime | None:
    """Parse the ISO-8601 UTC ``valid_time`` (None = latest); raise on a bad string."""
    if valid_time is None:
        return None
    if not isinstance(valid_time, str):
        raise router_input_error(
            spec.error_code_prefix, f"valid_time must be a string; got {type(valid_time).__name__}",
            spec.input_error_suffix)
    s = valid_time.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = _dt.datetime.fromisoformat(s)
    except ValueError as exc:
        raise router_input_error(
            spec.error_code_prefix, f"valid_time={valid_time!r} is not a parseable ISO-8601 string",
            spec.input_error_suffix) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return dt.astimezone(_dt.timezone.utc)


def _product_prefix(acc: str) -> str:
    return f"CONUS/MultiSensor_QPE_{acc}_{_QPE_PASS}_00.00/"


def _targeted_keys(acc: str, target: _dt.datetime) -> list[str]:
    """The ordered candidate S3 keys for a targeted valid_time (nearest-earlier walk)."""
    top = target.replace(minute=0, second=0, microsecond=0)
    keys: list[str] = []
    for hours_back in range(0, _WALKBACK_HOURS + 1):
        cand = top - _dt.timedelta(hours=hours_back)
        yyyymmdd = cand.strftime("%Y%m%d")
        hhmmss = cand.strftime("%H0000")
        keys.append(
            f"{_product_prefix(acc)}{yyyymmdd}/"
            f"MRMS_MultiSensor_QPE_{acc}_{_QPE_PASS}_00.00_{yyyymmdd}-{hhmmss}.grib2.gz")
    return keys


def _list_url(base: str, prefix: str, max_keys: int) -> str:
    from urllib.parse import quote
    return f"{base}/?list-type=2&prefix={quote(prefix)}&max-keys={max_keys}"


@_hooks.register_hook("mrms_qpe.resolve_build")
def resolve_build(spec: SourceSpec, params: dict[str, Any]) -> list["_hooks.RequestPlan"]:
    """Emit the candidate S3 list-object probes for the requested key (single round)."""
    acc = _normalize_accumulation(spec, params.get("accumulation", "24h"))
    vt = _parse_valid_time(spec, params.get("valid_time"))
    endpoint = spec.endpoints.get("data") or next(iter(spec.endpoints.values()))
    base = (endpoint.url or endpoint.url_template or "").rstrip("/")
    ua = {"User-Agent": spec.auth.user_agent}
    if vt is None:
        # latest: list the last N date directories; parse picks the max key.
        today = _dt.datetime.now(_dt.timezone.utc).date()
        plans = []
        for days_back in range(_LATEST_DATE_DIRS):
            d = today - _dt.timedelta(days=days_back)
            prefix = f"{_product_prefix(acc)}{d.strftime('%Y%m%d')}/"
            plans.append(_hooks.RequestPlan(url=_list_url(base, prefix, 1000), headers=ua))
        return plans
    # targeted: one exact-key probe per hour of the walkback; parse picks first-present.
    return [
        _hooks.RequestPlan(url=_list_url(base, key, 1), headers=ua)
        for key in _targeted_keys(acc, vt)
    ]


@_hooks.register_hook("mrms_qpe.resolve_parse")
def resolve_parse(spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]) -> dict[str, Any]:
    """Scrape the list bodies + pick the resolved key (latest=max, targeted=first-present)."""
    sc = spec.error_code_prefix
    acc = _normalize_accumulation(spec, params.get("accumulation", "24h"))
    vt = _parse_valid_time(spec, params.get("valid_time"))
    texts = [b.decode("utf-8", errors="replace") if isinstance(b, (bytes, bytearray)) else "" for b in bodies]
    if vt is None:
        found: list[str] = []
        for t in texts:
            found.extend(_KEY_RE.findall(t))
        if not found:
            raise router_upstream_error(
                sc, f"MRMS QPE {acc} {_QPE_PASS} bucket has no published files in the last "
                f"{_LATEST_DATE_DIRS} day(s)")
        return {"_grib_key": max(found)}
    # targeted: the i-th probe body carries its exact key iff that hour is published.
    cand_keys = _targeted_keys(acc, vt)
    for key, t in zip(cand_keys, texts):
        if f"<Key>{key}</Key>" in t:
            return {"_grib_key": key}
    raise router_not_available_error(
        sc, f"no MRMS QPE {acc} {_QPE_PASS} file found within {_WALKBACK_HOURS} h before "
        f"valid_time={vt.isoformat()}; the bucket may have a gap or the timestamp may be too "
        f"recent (Pass2 is delayed ~2 h)")
