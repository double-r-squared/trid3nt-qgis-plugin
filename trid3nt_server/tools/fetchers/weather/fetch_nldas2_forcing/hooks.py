"""fetch_nldas2_forcing record hook: NASA NLDAS-2 hourly land-surface forcing.

``pynldas2`` owns the GES DISC time-series service - the per-variable URL set, the
1/8 degree cell grid the AOI expands to, the ascii decode and the assembly into an
xarray Dataset. Three things it does not own ride here.

THE LOGIN IS CHECKED BEFORE THE READ, not after. GES DISC answers an
unauthenticated request 200 WITH ITS SIGN-IN PAGE, which the library's ascii parser
reads as malformed data - so an absent credential would surface as a parse error
about column counts rather than as the missing account it is. The check names the
file and the host instead.

THE VARIABLE VOCABULARY and its units are read off the library's own table rather
than restated, so they cannot drift from what the service sends; an unknown name is
refused by name rather than sent to the service to come back empty.

THE DELIVERABLE IS A FORCING SERIES: each requested variable averaged over the AOI
into an hourly series, plus the precipitation accumulation, as a bare JSON record
rather than a renderable layer.
"""

from __future__ import annotations

import datetime as _dt
import os
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..._router.errors import router_empty_error, router_input_error, router_upstream_error
from ..._router.hooks import register_hook
from ..._router.hooks.hyriver import hyriver_call

__all__ = ["build_record"]

#: The Earthdata Login host every GES DISC read redirects to.
_EDL_HOST = "urs.earthdata.nasa.gov"

#: Asked for nothing in particular, a forcing question wants rain and temperature.
_DEFAULT_VARIABLES = ("prcp", "temp")


def _vocabulary() -> dict[str, dict[str, str]]:
    """The forcing vocabulary and its units, read off the library's own table.

    Restating the units here would let them drift from what the service sends.
    """
    from pynldas2.pynldas2 import NLDAS2_VARS

    return NLDAS2_VARS


def _require_earthdata_login(spec: SourceSpec) -> None:
    """Refuse by name until ``~/.netrc`` carries credentials for Earthdata Login."""
    import netrc as _netrc

    path = os.path.join(os.path.expanduser("~"), ".netrc")
    try:
        if _netrc.netrc(path).authenticators(_EDL_HOST) is not None:
            return
    except (OSError, _netrc.NetrcParseError):
        pass
    raise router_upstream_error(
        spec.error_code_prefix,
        f"NLDAS-2 is served from NASA GES DISC behind {_EDL_HOST}, which answers an "
        f"unauthenticated request 200 with its sign-in page; add a machine entry for "
        f"{_EDL_HOST} to {path} (an Earthdata Login account, with the GES DISC "
        "application authorized, is an interactive step)",
    )


def _variables(spec: SourceSpec, params: dict[str, Any]) -> list[str]:
    """Validate the requested variable set against the library's own vocabulary."""
    vocab = _vocabulary()
    raw = params.get("variables") or _DEFAULT_VARIABLES
    out: list[str] = []
    for v in raw:
        name = str(v).strip().lower()
        if name not in vocab:
            raise router_input_error(
                spec.error_code_prefix,
                f"variable {v!r} is not an NLDAS-2 forcing variable {sorted(vocab)}",
                spec.input_error_suffix,
            )
        if name not in out:
            out.append(name)
    return sorted(out)


@register_hook("nldas2_forcing.build_record")
def build_record(
    spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]
) -> dict[str, Any]:
    """Build the AOI-mean hourly forcing record over the request window."""
    import numpy as np
    import pynldas2

    sc = spec.error_code_prefix
    _require_earthdata_login(spec)

    w, s, e, n = (float(v) for v in params["bbox"])
    start = _dt.date.fromisoformat(str(params["start_date"]))
    end = _dt.date.fromisoformat(str(params["end_date"]))
    variables = _variables(spec, params)
    vocab = _vocabulary()

    ds = hyriver_call(
        spec,
        f"pynldas2.get_bygeom(bbox=({w}, {s}, {e}, {n}), {start}..{end}, {variables})",
        pynldas2.get_bygeom,
        (w, s, e, n),
        start.isoformat(),
        end.isoformat(),
        variables=variables,
    )

    space = [d for d in ("y", "x", "lat", "lon", "latitude", "longitude") if d in ds.dims]
    n_cells = 1
    for d in space:
        n_cells *= int(ds.sizes[d])
    mean = ds.mean(dim=space) if space else ds

    times = [str(np.datetime_as_string(t, unit="h")) + ":00" for t in ds["time"].values]
    if not times:
        raise router_empty_error(
            sc,
            f"no NLDAS-2 hours in {start.isoformat()}..{end.isoformat()} over bbox "
            f"{[w, s, e, n]}",
            spec.empty_error_suffix,
        )

    series: dict[str, Any] = {}
    for name in variables:
        if name not in mean:
            continue
        vals = np.asarray(mean[name].values, dtype="float64")
        series[name] = {
            "units": vocab[name]["units"],
            "long_name": vocab[name]["long_name"],
            "values": [round(float(v), 6) if np.isfinite(v) else None for v in vals],
        }

    record: dict[str, Any] = {
        "source": "NASA NLDAS-2 primary forcing (NLDAS_FORA0125_H v2.0)",
        "bbox": [w, s, e, n],
        "start": start.isoformat(),
        "end": end.isoformat(),
        "n_hours": len(times),
        "n_cells": n_cells,
        "times": times,
        "series": series,
    }
    if "prcp" in series:
        finite = [v for v in series["prcp"]["values"] if v is not None]
        record["precip_total_mm"] = round(float(sum(finite)), 3)
    return record
