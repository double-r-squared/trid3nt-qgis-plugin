#!/usr/bin/env python
"""proof: SWMM Snow Pack degree-day melt (rain-on-snow), REAL forcing.

Chart-first validation class -> charts/scalars, no raster. Temperature forcing is
REAL hourly KBUF (Buffalo NY) ASOS air temperature (fetch_asos_metar ``tmpf``)
over the January 2024 rain-on-snow event: a deep cold spell (Jan 14-21, snow
accumulates) then a warm-up above freezing (Jan 23-27, snowmelt + rain). Precip
is a representative winter sequence phased by the REAL temperature (snowfall while
sub-freezing, a rain burst during the warm-up); AORC hourly extraction over the
14-day window is the available upgrade path (documented, not required for the
mechanism proof).

Two panels into docs/proof/templates/ (named after the tool):
  * ..._swe_series.png    -- snow water equivalent: accumulation then degree-day
    melt, with the plowed (snow-removal) variant overlaid, and the real KBUF
    temperature + the 32 F freeze line on a twin axis.
  * ..._runoff_snowmelt_vs_rainonly.png -- runoff hydrograph, snowmelt physics
    vs the rain-only (climate-naive) control.
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from trid3nt_server.workflows.swmm.snowmelt_degree_day.snowmelt_degree_day import (
    build_snowmelt_inp, solve_snowmelt_deck, _total_melt_in, _peak,
)

OUT = "/home/nate/Documents/trid3nt-local/docs/proof/templates"
STEM = "swmm_snowmelt_degree_day"
KBUF_BBOX = [-78.9, 42.8, -78.6, 43.05]
EVENT_START = "2024-01-14"
EVENT_END = "2024-01-28"
DT_MIN = 60
AREA_AC = 50.0
DIVIDE_F = 32.0


def fetch_real_kbuf_temperature() -> tuple[list[tuple[str, float]], list[float], list[float]]:
    """REAL hourly KBUF ASOS temperature resampled to a regular hourly grid.
    Returns (temperature_series [(clock, degF)], hours[], temp_f[])."""
    from trid3nt_server.data import TOOL_REGISTRY
    from trid3nt_server.data.cache import read_object_bytes_s3
    import fiona

    r = TOOL_REGISTRY["fetch_asos_metar"].fn(
        bbox=KBUF_BBOX, start_time=EVENT_START, end_time=EVENT_END)
    uri = r["uri"] if isinstance(r, dict) else r.uri
    b = read_object_bytes_s3(uri) if str(uri).startswith("s3://") else open(uri, "rb").read()
    p = tempfile.mktemp(suffix=".fgb")
    open(p, "wb").write(b)
    obs: list[tuple[datetime, float]] = []
    with fiona.open(p) as src:
        for f in src:
            pr = f["properties"]
            if pr.get("station") == "BUF" and pr.get("tmpf") is not None:
                obs.append((datetime.strptime(pr["valid"], "%Y-%m-%d %H:%M"),
                            float(pr["tmpf"])))
    obs.sort()
    t0 = obs[0][0]
    obs_h = [((t - t0).total_seconds() / 3600.0, v) for t, v in obs]
    n_hours = int(obs_h[-1][0]) + 1
    oh = np.array([x for x, _ in obs_h])
    ov = np.array([v for _, v in obs_h])
    temp_f = [float(np.interp(h, oh, ov)) for h in range(n_hours)]
    series = [(f"{h}:00", round(v, 2)) for h, v in enumerate(temp_f)]
    return series, list(range(n_hours)), temp_f


def build_precip_from_temperature(temp_f: list[float]) -> list[tuple[str, float]]:
    """Representative precip phased by the REAL temperature: steady snowfall on
    the cold accumulation days (Jan 15-20 window: hours 24-168, T sub-freezing),
    then a rain burst during the warm-up (hours 240-270)."""
    rain: list[tuple[str, float]] = []
    for h in range(len(temp_f)):
        if 24 <= h < 168 and temp_f[h] <= DIVIDE_F:
            r = 0.03            # steady lake-effect snowfall through the cold spell
        elif 240 <= h < 270:
            r = 0.10            # warm rain burst on the ripe snowpack
        else:
            r = 0.0
        rain.append((f"{h}:00", r))
    return rain


def main():
    temp_series, hours_t, temp_f = fetch_real_kbuf_temperature()
    rain_series = build_precip_from_temperature(temp_f)
    common = dict(dt_min=DT_MIN, area_ac=AREA_AC, base_temp_f=32.0,
                  percent_impervious=80.0, plow_fraction=0.90)

    hrs, swe, ro_snow, rn, cont = solve_snowmelt_deck(
        build_snowmelt_inp(temp_series, rain_series, dividing_temp_f=DIVIDE_F,
                           removal=False, **common))
    _, _, ro_rain, _, _ = solve_snowmelt_deck(
        build_snowmelt_inp(temp_series, rain_series, dividing_temp_f=-99.0,
                           removal=False, **common))
    _, swe_rem, ro_rem, _, _ = solve_snowmelt_deck(
        build_snowmelt_inp(temp_series, rain_series, dividing_temp_f=DIVIDE_F,
                           removal=True, plow_threshold_in=0.3, **common))

    hrs = np.array(hrs); swe = np.array(swe); swe_rem = np.array(swe_rem)
    ro_snow = np.array(ro_snow); ro_rain = np.array(ro_rain)
    peak_swe = float(swe.max()); melt = _total_melt_in(list(swe))
    snow_peak, snow_i = _peak(list(ro_snow)); rain_peak, _ = _peak(list(ro_rain))
    amp = snow_peak / rain_peak if rain_peak > 0 else 0.0
    rem_swe = float(swe_rem.max())

    # ---- (1) SWE series + real temperature twin axis --------------------------
    fig, ax = plt.subplots(figsize=(6.0, 2.8), dpi=100)
    ax.plot(hrs / 24, swe, color="#1f78b4", lw=1.8, label="snowpack SWE (no removal)")
    ax.plot(hrs / 24, swe_rem, color="#6a3d9a", lw=1.5, ls="--", label="snowpack SWE (plowed)")
    ax.set_xlabel("time (days from Jan 14, 2024)", fontsize=8)
    ax.set_ylabel("snow water equivalent (in)", fontsize=8, color="#1f78b4")
    ax.tick_params(labelsize=7)
    ax.set_ylim(0, max(peak_swe * 1.15, 0.1))
    ax2 = ax.twinx()
    l_temp, = ax2.plot(np.array(hours_t) / 24, temp_f, color="#e31a1c", lw=0.9,
                       alpha=0.7, label="KBUF air temp (real ASOS)")
    ax2.axhline(32, color="0.5", lw=0.8, ls=":")
    ax2.set_ylabel("air temperature (F)", fontsize=8, color="#e31a1c")
    ax2.tick_params(labelsize=7)
    ax2.text(11.6, 33.2, "32 F freeze / rain-snow split", fontsize=5.5, color="0.45")
    handles = [l for l in ax.get_lines()] + [l_temp]
    ax.legend(handles, [h.get_label() for h in handles], fontsize=6,
              frameon=False, loc="upper left")
    ax.set_title("Snow Pack degree-day melt on real KBUF (Buffalo) Jan 2024 forcing", fontsize=8)
    fig.text(0.5, 0.005,
             f"swmm_snowmelt_degree_day on REAL KBUF ASOS temperature (Jan 14-27 2024): "
             f"cold spell builds peak SWE {peak_swe:.2f} in, warm-up melts {melt:.2f} in; "
             f"plowing cuts peak SWE to {rem_swe:.2f} in. Continuity {cont:.2f}% (ADR 0218).",
             ha="center", fontsize=6, color="0.4", wrap=True)
    fig.tight_layout(rect=(0, 0.07, 1, 1))
    p1 = os.path.join(OUT, f"{STEM}_swe_series.png")
    fig.savefig(p1, dpi=200); plt.close(fig)
    print("wrote", p1)
    print(f"  peak SWE {peak_swe:.3f} in, total melt {melt:.3f} in, plowed peak SWE {rem_swe:.3f} in")

    # ---- (2) runoff snowmelt vs rain-only -------------------------------------
    fig, ax = plt.subplots(figsize=(6.0, 2.6), dpi=100)
    ax.plot(hrs / 24, ro_snow, color="#1f78b4", lw=1.8, label="snowmelt physics")
    ax.plot(hrs / 24, ro_rain, color="#33a02c", lw=1.4, ls="--", label="rain-only (climate-naive)")
    ax.set_xlabel("time (days from Jan 14, 2024)", fontsize=8)
    ax.set_ylabel("runoff (cfs)", fontsize=8)
    ax.tick_params(labelsize=7)
    ax.legend(fontsize=6.5, frameon=False, loc="upper left")
    ax.grid(True, alpha=0.25)
    ax.set_title("Rain-on-snow runoff: snowmelt physics vs rain-only", fontsize=8)
    fig.text(0.5, 0.005,
             f"Snowmelt STORES the cold-spell precip and releases it with the warm rain: "
             f"peak {snow_peak:.2f} cfs at day {hrs[snow_i]/24:.1f} vs rain-only {rain_peak:.2f} cfs "
             f"({amp:.2f}x), and rain-only fabricates mid-winter runoff the snowpack withheld "
             f"(ADR 0218; EPA SWMM degree-day snowmelt).",
             ha="center", fontsize=6, color="0.4", wrap=True)
    fig.tight_layout(rect=(0, 0.07, 1, 1))
    p2 = os.path.join(OUT, f"{STEM}_runoff_snowmelt_vs_rainonly.png")
    fig.savefig(p2, dpi=200); plt.close(fig)
    print("wrote", p2)
    print(f"  snowmelt peak {snow_peak:.3f} cfs, rain-only peak {rain_peak:.3f} cfs, amp {amp:.3f}")

    # physics assertions
    assert peak_swe > 0.1, "no snow accumulated"
    assert melt > 0.1, "no snowmelt"
    assert amp > 1.0, "rain-on-snow did not amplify the peak"
    assert rem_swe < peak_swe, "plowing did not reduce the snowpack"
    assert cont < 5.0, "continuity error too high"
    print("  PHYSICS ASSERTIONS PASS")


if __name__ == "__main__":
    main()
