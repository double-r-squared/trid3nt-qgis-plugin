"""Ingest a USGS finite-fault product (ComCat) into a normalized N-subfault table
-- the finite-fault UPGRADE to the Okada-dtopo front.

The single-subfault Okada synthesis (``earthquake_source`` + the worker's synthetic
``maketopo``) renders as ONE idealized rectangle -- a straight uplift bar. A REAL
earthquake's seafloor deformation is the superposition of the slip on MANY subfault
patches from a published finite-fault INVERSION. This module fetches the USGS
finite-fault product for a ComCat event, parses the SRCMOD-style ``.fsp``
complete-inversion file into a normalized patch table (lon / lat / depth / strike /
dip / rake / slip / length / width), and serializes it in clawpack ``dtopotools``
``CSVFault`` column format so the worker assembles a MULTI-subfault
``dtopotools.Fault`` -> a real, concentrated, asymmetric Okada deformation field.

Fallback ladder (the data-source fallback norm -- degrade primary -> fallback ->
honest typed error, never silent):
  * finite-fault product PRESENT  -> ``basis="measured-inversion"``, naming the
    product id + version + URL (the slip is a published inversion, not a scaling
    law); the worker builds N subfaults.
  * finite-fault product ABSENT   -> the single-subfault Wells & Coppersmith
    scaling synthesis (the existing path) is the DEGRADE rung, LOUDLY labeled
    ``basis="derived"`` on the envelope.

``parse_fsp`` is a PURE parser (unit-testable on a cached ``.fsp`` fixture); the
ComCat product query + file download is the I/O boundary (``fetch_finite_fault_model``),
monkeypatchable in tests. No FDSN / MinIO here.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.request
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(
    "trid3nt_server.workflows.geoclaw.finite_fault")

__all__ = [
    "FiniteFaultPatch",
    "FiniteFaultModel",
    "FiniteFaultError",
    "parse_fsp",
    "to_csvfault_text",
    "fetch_finite_fault_model",
    "COMCAT_EVENT_DETAIL_URL",
]

#: ComCat event-detail (products) endpoint. The finite-fault product carries the
#: ``complete_inversion.fsp`` content the parser consumes.
COMCAT_EVENT_DETAIL_URL = (
    "https://earthquake.usgs.gov/fdsnws/event/1/query?eventid={eid}&format=geojson"
)

#: Preferred finite-fault content file, in order: the full SRCMOD-style inversion
#: (per-subfault LAT/LON/Z/SLIP/RAKE) is the parseable one.
_FSP_CONTENT_KEYS = ("complete_inversion.fsp", "basic_inversion.param")

_HTTP_TIMEOUT_S = 30.0


class FiniteFaultError(RuntimeError):
    """A typed finite-fault ingestion failure (never a silent dead-end)."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


@dataclass
class FiniteFaultPatch:
    """One rectangular subfault patch of a finite-fault inversion.

    Geometry is in dtopotools standard units: metres for length / width / depth,
    degrees for strike / dip / rake, metres for slip. ``lon`` / ``lat`` are the
    patch CENTROID (the FSP convention -- "coordinates given for center of each
    subfault"), so ``coordinate_specification="centroid"`` downstream."""

    lon: float
    lat: float
    depth_m: float
    strike_deg: float
    dip_deg: float
    rake_deg: float
    slip_m: float
    length_m: float
    width_m: float


@dataclass
class FiniteFaultModel:
    """A parsed finite-fault inversion: N patches + the inversion provenance.

    ``patches`` carry the real inverted slip distribution; ``product_*`` name the
    exact ComCat product (id + version + URL) so the run's envelope can cite the
    measured inversion (``basis="measured-inversion"``)."""

    patches: list[FiniteFaultPatch]
    magnitude: float | None = None
    event_tag: str | None = None
    n_along_strike: int | None = None
    n_down_dip: int | None = None
    product_id: str | None = None
    product_version: str | None = None
    product_url: str | None = None
    fsp_url: str | None = None
    _footprint: tuple[float, float, float, float] | None = field(default=None, repr=False)

    @property
    def n_subfaults(self) -> int:
        return len(self.patches)

    @property
    def max_slip_m(self) -> float:
        return max((p.slip_m for p in self.patches), default=0.0)

    @property
    def min_slip_m(self) -> float:
        return min((p.slip_m for p in self.patches), default=0.0)

    @property
    def footprint_bbox(self) -> tuple[float, float, float, float]:
        """The (min_lon, min_lat, max_lon, max_lat) enclosing all patch centroids."""
        if self._footprint is not None:
            return self._footprint
        lons = [p.lon for p in self.patches]
        lats = [p.lat for p in self.patches]
        return (min(lons), min(lats), max(lons), max(lats))

    @property
    def centroid_lonlat(self) -> tuple[float, float]:
        """Slip-weighted centroid of the rupture (the representative epicentre for
        domain placement)."""
        w = sum(max(p.slip_m, 0.0) for p in self.patches)
        if w <= 0.0:
            lons = [p.lon for p in self.patches]
            lats = [p.lat for p in self.patches]
            return (sum(lons) / len(lons), sum(lats) / len(lats))
        clon = sum(p.lon * max(p.slip_m, 0.0) for p in self.patches) / w
        clat = sum(p.lat * max(p.slip_m, 0.0) for p in self.patches) / w
        return (clon, clat)

    @property
    def provenance_label(self) -> str:
        pid = self.product_id or "?"
        ver = f" v{self.product_version}" if self.product_version else ""
        return (
            f"USGS finite-fault product {pid}{ver}: {self.n_subfaults} subfaults, "
            f"slip {self.min_slip_m:.2f}-{self.max_slip_m:.2f} m"
            f"{f' (Mw {self.magnitude:.2f})' if self.magnitude is not None else ''}"
        )


def _hval(text: str, pattern: str) -> str | None:
    m = re.search(pattern, text)
    return m.group(1) if m else None


def parse_fsp(text: str) -> FiniteFaultModel:
    """Parse a SRCMOD-style finite-fault ``.fsp`` inversion into a patch table.

    Reads the whole-fault mechanism (``% Mech : STRK = .. DIP = ..``) and the
    subfault dimensions (``% Invs : Dx = .. km  Dz = .. km``) from the comment
    header, locates the per-subfault DATA column header
    (``% LAT LON X==EW Y==NS Z SLIP RAKE ...``) to index the LAT / LON / Z / SLIP /
    RAKE columns robustly, and builds one ``FiniteFaultPatch`` per data row (depth
    km -> m, subfault length = Dx, width = Dz). A per-row strike / dip / length /
    width column, when present (multi-segment files), overrides the header value.

    Pure -- no I/O. Raises ``FiniteFaultError`` on an unparseable file."""
    lines = text.splitlines()

    strike_h = _hval(text, r"STRK\s*=\s*([-\d.]+)")
    dip_h = _hval(text, r"\bDIP\s*=\s*([-\d.]+)")
    dx_km = _hval(text, r"\bDx\s*=\s*([\d.]+)\s*km")
    dz_km = _hval(text, r"\bDz\s*=\s*([\d.]+)\s*km")
    mw = _hval(text, r"\bMw\s*=\s*([\d.]+)")
    nx = _hval(text, r"\bNx\s*=\s*(\d+)")
    nz = _hval(text, r"\bNz\s*=\s*(\d+)")
    event_tag = _hval(text, r"EventTAG:\s*([^\s%]+)")

    if strike_h is None or dip_h is None:
        raise FiniteFaultError(
            "FINITE_FAULT_FSP_NO_MECHANISM",
            "finite-fault .fsp header carries no STRK/DIP mechanism line",
        )
    strike = float(strike_h)
    dip = float(dip_h)
    # Subfault along-strike length (Dx) and down-dip width (Dz), km -> m. When the
    # header omits them, fall back to a nominal 10 km patch (the FSP always carries
    # them for a gridded inversion; the fallback keeps a degenerate file parseable).
    length_m = float(dx_km) * 1000.0 if dx_km else 10_000.0
    width_m = float(dz_km) * 1000.0 if dz_km else 10_000.0

    # Locate the DATA column header so the column indices are read from the file,
    # not assumed. The real header is the comment line naming LAT + LON + SLIP as
    # standalone column tokens (NOT the "% Loc : LAT = .. LON = .." scalar line,
    # which lacks SLIP); take the LAST such line (it sits just above the data).
    col_idx: dict[str, int] = {}
    for ln in lines:
        s = ln.lstrip()
        if not s.startswith("%"):
            continue
        toks = [t.upper().split("==")[0] for t in s.lstrip("%").split()]  # "X==EW" -> "X"
        if "LAT" in toks and "LON" in toks and "SLIP" in toks:
            found: dict[str, int] = {}
            for i, key in enumerate(toks):
                if key in ("LAT", "LON", "Z", "SLIP", "RAKE", "STRIKE", "DIP") and key not in found:
                    found[key] = i
            col_idx = found  # last matching header wins
    if "LAT" not in col_idx or "LON" not in col_idx or "SLIP" not in col_idx:
        # Header column line absent -> assume the canonical SRCMOD order
        # LAT LON X Y Z SLIP RAKE TRUP RISE SF_MOMENT.
        col_idx = {"LAT": 0, "LON": 1, "Z": 4, "SLIP": 5, "RAKE": 6}

    patches: list[FiniteFaultPatch] = []
    for ln in lines:
        s = ln.strip()
        if not s or s.startswith("%"):
            continue
        toks = s.split()
        try:
            lat = float(toks[col_idx["LAT"]])
            lon = float(toks[col_idx["LON"]])
            slip = float(toks[col_idx["SLIP"]])
        except (IndexError, ValueError):
            continue
        depth_km = float(toks[col_idx["Z"]]) if "Z" in col_idx and col_idx["Z"] < len(toks) else 10.0
        rake = float(toks[col_idx["RAKE"]]) if "RAKE" in col_idx and col_idx["RAKE"] < len(toks) else 90.0
        p_strike = float(toks[col_idx["STRIKE"]]) if "STRIKE" in col_idx and col_idx["STRIKE"] < len(toks) else strike
        p_dip = float(toks[col_idx["DIP"]]) if "DIP" in col_idx and col_idx["DIP"] < len(toks) else dip
        patches.append(FiniteFaultPatch(
            lon=lon, lat=lat, depth_m=depth_km * 1000.0,
            strike_deg=p_strike, dip_deg=p_dip, rake_deg=rake,
            slip_m=slip, length_m=length_m, width_m=width_m,
        ))

    if not patches:
        raise FiniteFaultError(
            "FINITE_FAULT_FSP_NO_SUBFAULTS",
            "finite-fault .fsp carried no parseable subfault rows",
        )
    return FiniteFaultModel(
        patches=patches,
        magnitude=float(mw) if mw else None,
        event_tag=event_tag,
        n_along_strike=int(nx) if nx else None,
        n_down_dip=int(nz) if nz else None,
    )


def to_csvfault_text(model: FiniteFaultModel) -> str:
    """Serialize a finite-fault model as clawpack ``dtopotools.CSVFault`` CSV text.

    The header names the columns ``CSVFault.read`` maps (units in parentheses ->
    dtopotools standard units), so the worker reads it with
    ``CSVFault().read(path, coordinate_specification="centroid")`` and builds the
    N-subfault Okada source with no bespoke parsing. Metres / degrees throughout."""
    out = [
        "longitude,latitude,depth(m),length(m),width(m),strike,dip,rake,slip(m)"
    ]
    for p in model.patches:
        out.append(
            f"{p.lon:.6f},{p.lat:.6f},{p.depth_m:.3f},{p.length_m:.3f},"
            f"{p.width_m:.3f},{p.strike_deg:.3f},{p.dip_deg:.3f},"
            f"{p.rake_deg:.3f},{p.slip_m:.6f}"
        )
    return "\n".join(out) + "\n"


def _http_get(url: str) -> bytes:
    """Fetch a URL's bytes (stdlib urllib; the finite-fault product is a public
    static file with no repo catalog driver -- unlike the FDSN summary feed).
    Isolated so tests monkeypatch it."""
    req = urllib.request.Request(url, headers={"User-Agent": "trid3nt-geoclaw/1"})
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:  # noqa: S310
        return resp.read()


def _select_finite_fault_product(detail: dict[str, Any]) -> dict[str, Any] | None:
    """From a ComCat event-detail geojson, pick the finite-fault product with the
    most subfaults (prefer the latest 'us' inversion). Returns the product dict or
    None when the event carries no finite-fault product."""
    products = (detail.get("properties") or {}).get("products") or {}
    ff = products.get("finite-fault") or []
    if not ff:
        return None
    # Prefer the most-recently-updated finite-fault product.
    def _upd(p: dict[str, Any]) -> int:
        try:
            return int(p.get("updateTime") or 0)
        except (TypeError, ValueError):
            return 0
    return sorted(ff, key=_upd)[-1]


def fetch_finite_fault_model(
    event_id: str,
    *,
    _http_get_fn: Any = None,
) -> FiniteFaultModel | None:
    """Fetch + parse the USGS finite-fault product for a ComCat event.

    Queries the ComCat event-detail endpoint for the event's ``finite-fault``
    product, downloads its ``complete_inversion.fsp`` (falling back to
    ``basic_inversion.param``), parses it, and stamps the product provenance
    (id / version / URL). Returns ``None`` -- NOT an error -- when the event carries
    no finite-fault product (the degrade rung: the caller falls back to the
    single-subfault scaling synthesis, loudly labeled). Raises ``FiniteFaultError``
    only on a product that IS present but unparseable / unreachable.

    ``_http_get_fn`` overrides the URL fetch for offline tests."""
    http_get = _http_get_fn or _http_get
    if not event_id:
        return None
    url = COMCAT_EVENT_DETAIL_URL.format(eid=event_id)
    try:
        detail = json.loads(http_get(url).decode("utf-8", errors="replace"))
    except Exception as exc:  # noqa: BLE001 - event detail unreachable => no product
        logger.info(
            "fetch_finite_fault_model: ComCat detail unreachable for %s (%s); "
            "no finite-fault product -> single-subfault fallback", event_id, exc,
        )
        return None

    product = _select_finite_fault_product(detail)
    if product is None:
        logger.info(
            "fetch_finite_fault_model: event %s carries no finite-fault product "
            "-> single-subfault scaling fallback", event_id,
        )
        return None

    contents = product.get("contents") or {}
    fsp_url: str | None = None
    for key in _FSP_CONTENT_KEYS:
        entry = contents.get(key)
        if isinstance(entry, dict) and entry.get("url"):
            fsp_url = str(entry["url"])
            break
    if not fsp_url:
        raise FiniteFaultError(
            "FINITE_FAULT_NO_FSP",
            f"finite-fault product {product.get('code')!r} for event {event_id} "
            f"has no complete_inversion.fsp / basic_inversion.param content",
        )
    try:
        fsp_text = http_get(fsp_url).decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001 - the product IS present but unreachable
        raise FiniteFaultError(
            "FINITE_FAULT_FSP_FETCH_FAILED",
            f"could not download finite-fault {fsp_url}: {exc}",
        ) from exc

    model = parse_fsp(fsp_text)
    model.product_id = str(product.get("code") or "")
    model.product_version = str(product.get("version") or "") or None
    model.fsp_url = fsp_url
    model.product_url = (
        f"https://earthquake.usgs.gov/product/finite-fault/{model.product_id}"
        if model.product_id else None
    )
    logger.info(
        "fetch_finite_fault_model event=%s -> %s (footprint=%s)",
        event_id, model.provenance_label, model.footprint_bbox,
    )
    return model
