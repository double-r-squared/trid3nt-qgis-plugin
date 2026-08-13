"""Published-deck runner - shared machinery for the CITED-EXAMPLE SWMM templates.

Where ``swmm_urban_flood`` SYNTHESIZES a quasi-2D mesh from a DEM and
``swmm_network_import`` parses a REAL municipal GIS network, this core ingests a
specific CITED, PUBLISHED SWMM ``.inp`` deck (an openswmm.org example model,
authored by a named practitioner), runs it VERBATIM through the same headless
``swmm5_run`` solver the network family uses, and postprocesses the deck-relevant
outputs into CHARTS + typed scalars.

Demonstration-honesty (loud, per the wave's labeling class): the deck is the
CITED EXAMPLE's network, NOT a user AOI. Coordinates in these textbook decks are
SCHEMATIC (local model units, not lon/lat), so this runner emits CHARTS
(hydrographs / stage recession / pollutographs / control tracking) + typed
scalars - never a georeferenced map layer. Every number the agent narrates comes
from the typed result the postprocess computed (invariant 1); nothing is
free-generated.

Sourcing (ADR 0128): the author-posted decks are fetched AT RUNTIME from the
pinned public source page (redistribution of a named author's forum deck is not a
license we can assume, so we do NOT bake the deck into the repo). The public
topic page renders the full deck inline; the runner extracts it deterministically.
A fetch/parse/solve failure is an HONEST typed ``SWMM_DECK_*`` error, never a
silent dead-end or an invented result.
"""

from __future__ import annotations

import html as _html
import io
import logging
import re
import tempfile
import urllib.request
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("trid3nt_server.agent.mesh.swmm_deck_runner")

__all__ = [
    "SWMMDeckError",
    "PublishedDeck",
    "PUBLISHED_DECKS",
    "DeckSolveResult",
    "fetch_deck_text",
    "extract_inline_deck",
    "apply_rain_scale",
    "solve_deck_text",
    "node_depth_series",
    "link_flow_series",
    "subcatchment_runoff_series",
]


# --------------------------------------------------------------------------- #
# Typed error (shares the A.6 open-set error_code shape with SWMMNetworkError).
# --------------------------------------------------------------------------- #
class SWMMDeckError(RuntimeError):
    """Raised on any published-deck fetch / parse / solve failure.

    Carries an open-set ``error_code`` the agent emitter renders as a typed
    error frame. Codes:

    - ``SWMM_DECK_UNAVAILABLE`` - the pinned public source could not be reached
      or the inline deck could not be extracted (fetch-at-runtime miss).
    - ``SWMM_DECK_PARSE_FAILED`` - the extracted text is not a usable ``.inp``.
    - ``SWMM_DECK_RUN_FAILED`` - ``swmm5_run`` raised on the deck.
    - ``SWMM_DECK_CONTINUITY_UNREADABLE`` - no Flow-Routing Continuity in the
      ``.rpt`` (the run did not complete).
    - ``SWMM_MASS_BALANCE_EXCEEDED`` - the honesty gate: continuity over tolerance.
    - ``SWMM_DECK_DEPENDENCY_MISSING`` - swmm-api / swmm-toolkit unavailable.
    """

    error_code: str = "SWMM_DECK_FAILED"

    def __init__(
        self,
        error_code: str,
        *,
        message: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message or error_code)
        self.error_code = error_code
        self.details: dict[str, Any] = dict(details or {})


# --------------------------------------------------------------------------- #
# The cited-deck registry - each row is the CITATION + the honest run knobs.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PublishedDeck:
    """One cited published deck + the honest facts a template binds to.

    Fields:
        deck_id: stable internal id.
        title: the cited example's published title (verbatim).
        author: the named author of the published deck.
        source: a short human label for the hosting collection.
        source_url: the PINNED public page the deck is fetched from at runtime.
        thread_id: the openswmm topic id (provenance).
        forcing: what drives the deck - "rainfall" (a hyetograph TIMESERIES),
            "initial_storage" (pond drain-down, no external inflow), or
            "dry_weather_flow" (a DWF/INFLOWS pattern). Determines the honest
            editable knob AND the headline chart.
        mass_balance_tol_pct: the continuity honesty gate for THIS deck (looser
            for big master models than a tiny WQ deck).
        select_index: for a multi-deck page, which inline block to take (0-based).
        capabilities: the published capabilities this deck demonstrates (LID,
            storage-routing, PID/RTC) - the reason it is a distinct template.
        rain_scalable: True iff a rain-scale override is honestly meaningful
            (only rainfall-forced decks).
    """

    deck_id: str
    title: str
    author: str
    source: str
    source_url: str
    thread_id: int
    forcing: str
    mass_balance_tol_pct: float = 10.0
    select_index: int = 0
    capabilities: tuple[str, ...] = ()
    rain_scalable: bool = False
    note: str = ""


#: The cited decks landed by ADR 0128 (openswmm.org example models). Fetched at
#: runtime from ``source_url``; NOT baked (author-posted, redistribution unclear).
PUBLISHED_DECKS: dict[str, PublishedDeck] = {
    "lid_raingarden_wq": PublishedDeck(
        deck_id="lid_raingarden_wq",
        title=(
            "A Very Simple Two-Subcatchment Water Quality Model With and Without "
            "Rain Gardens"
        ),
        author="Robert Dickinson",
        source="openswmm.org example models",
        source_url=(
            "https://www.openswmm.org/Topic/15609/a-very-simple-two-subcatchment-"
            "water-quality-model-with-and-without-rain-gardens"
        ),
        thread_id=15609,
        forcing="rainfall",
        mass_balance_tol_pct=10.0,
        capabilities=("LID bioretention (rain garden)", "buildup/washoff water quality"),
        rain_scalable=True,
        note=(
            "Paired subcatchments: one WITH a rain-garden LID control, one WITHOUT. "
            "The LID subcatchment must show lower runoff and lower pollutant washoff "
            "- the built-in expected-outcome check."
        ),
    ),
    "wwtp_detention_ponds": PublishedDeck(
        deck_id="wwtp_detention_ponds",
        title="UV Plant with Detention Ponds",
        author="Rob James",
        source="openswmm.org example models",
        source_url="https://www.openswmm.org/Topic/14400/uv-plant-with-detention-ponds",
        thread_id=14400,
        forcing="initial_storage",
        mass_balance_tol_pct=10.0,
        capabilities=("stage-storage detention ponds", "weir/orifice storage routing"),
        rain_scalable=False,
        note=(
            "Storage (detention pond) drain-down demonstration driven by the ponds' "
            "initial depths routing through weirs - a storage-routing stress test, "
            "NOT a storm-response calibration target (the example publishes no "
            "numeric results)."
        ),
    ),
    "pump_pid_rtc": PublishedDeck(
        deck_id="pump_pid_rtc",
        title="Example - PID Control for a Pump",
        author="Robert Dickinson",
        source="openswmm.org example models (EXTRAN 3/4 composite)",
        source_url="https://www.openswmm.org/Topic/10082/example-pid-control-for-a-pump",
        thread_id=10082,
        forcing="dry_weather_flow",
        mass_balance_tol_pct=15.0,
        capabilities=("PID real-time control rule", "pump / wet-well regulation"),
        rain_scalable=False,
        note=(
            "A PID CONTROLS rule adjusts the pump SETTING to hold the wet-well "
            "(upstream node) at a 3 ft target depth under dry-weather + wet-weather "
            "inflow - a real-time-control (RTC) demonstration."
        ),
    ),
}


# --------------------------------------------------------------------------- #
# Fetch + inline extraction from the pinned public page.
# --------------------------------------------------------------------------- #
#: The SWMM section headers a deck is composed of (superset). The extractor keeps
#: a contiguous run of these; the first non-SWMM header ends the deck.
_SWMM_SECTIONS: frozenset[str] = frozenset({
    "TITLE", "OPTIONS", "EVAPORATION", "TEMPERATURE", "RAINGAGES", "SUBCATCHMENTS",
    "SUBAREAS", "INFILTRATION", "LID_CONTROLS", "LID_USAGE", "AQUIFERS",
    "GROUNDWATER", "GWF", "SNOWPACKS", "JUNCTIONS", "OUTFALLS", "DIVIDERS",
    "STORAGE", "CONDUITS", "PUMPS", "ORIFICES", "WEIRS", "OUTLETS", "XSECTIONS",
    "TRANSECTS", "STREETS", "INLETS", "INLET_USAGE", "LOSSES", "CONTROLS",
    "POLLUTANTS", "LANDUSES", "COVERAGES", "LOADINGS", "BUILDUP", "WASHOFF",
    "TREATMENT", "INFLOWS", "DWF", "RDII", "HYDROGRAPHS", "CURVES", "TIMESERIES",
    "PATTERNS", "REPORT", "TAGS", "MAP", "COORDINATES", "VERTICES", "POLYGONS",
    "SYMBOLS", "LABELS", "BACKDROP", "PROFILES", "FILES", "EVENTS", "ADJUSTMENTS",
    "RAINFALL", "SEGMENTS",
})

_HDR_RE = re.compile(r"^\s*\[([A-Z_0-9]+)\]\s*$")


def extract_inline_deck(page_html: str, *, select_index: int = 0) -> str:
    """Extract a SWMM ``.inp`` deck from a public topic page's inline render.

    The openswmm topic page renders the full deck text inside a code block. We
    strip HTML tags, unescape entities, then take the contiguous run of KNOWN
    SWMM sections starting at the ``select_index``-th ``[OPTIONS]`` (its preceding
    ``[TITLE]`` when present). Forum prose that resumes after the deck is trimmed:
    a non-SWMM section header ends the block, and a runaway of sentence-like lines
    breaks out. Deterministic (no network, no LLM).

    Raises ``SWMMDeckError('SWMM_DECK_PARSE_FAILED')`` when no deck block is found.
    """
    txt = _html.unescape(re.sub(r"<[^>]+>", "", page_html))
    lines = txt.splitlines()

    opt_idx = [
        i for i, ln in enumerate(lines)
        if (m := _HDR_RE.match(ln)) and m.group(1) == "OPTIONS"
    ]
    title_idx = [
        i for i, ln in enumerate(lines)
        if (m := _HDR_RE.match(ln)) and m.group(1) == "TITLE"
    ]
    if not opt_idx:
        raise SWMMDeckError(
            "SWMM_DECK_PARSE_FAILED",
            message="no [OPTIONS] section found in the source page (deck not inline)",
        )
    if select_index >= len(opt_idx):
        raise SWMMDeckError(
            "SWMM_DECK_PARSE_FAILED",
            message=(
                f"requested deck block {select_index} but the page has only "
                f"{len(opt_idx)} deck(s)"
            ),
        )

    this_opt = opt_idx[select_index]
    # start at the [TITLE] immediately preceding this [OPTIONS], else at [OPTIONS].
    preceding_titles = [t for t in title_idx if t < this_opt]
    start = preceding_titles[-1] if preceding_titles else this_opt
    # hard end bound: the NEXT deck's start (so a two-deck page never bleeds).
    next_opt = opt_idx[select_index + 1] if select_index + 1 < len(opt_idx) else None
    next_title = min((t for t in title_idx if t > this_opt), default=None)
    hard_end = min(
        [b for b in (next_opt, next_title) if b is not None and b > start],
        default=len(lines),
    )

    out: list[str] = []
    consec_prose = 0
    cur: str | None = None
    for ln in lines[start:hard_end]:
        m = _HDR_RE.match(ln)
        if m:
            if m.group(1) in _SWMM_SECTIONS:
                out.append(ln)
                cur = m.group(1)
                consec_prose = 0
                continue
            break  # a non-SWMM header ends the deck
        s = ln.strip()
        # A digit-free, sentence-like line OUTSIDE the free-text [TITLE] section is
        # forum prose that resumed after the deck - deck data rows carry digits/ids.
        # This ends the block immediately (short footers included).
        is_prose = bool(
            s
            and not s.startswith(";")
            and re.search(r"[.!?]$", s)
            and not any(c.isdigit() for c in s)
            and len(s.split()) >= 3
        )
        if cur != "TITLE" and is_prose:
            break
        if consec_prose > 40:
            break
        out.append(ln)
        if s and not s.startswith(";") and re.search(r"[.!?]$", s) and len(s.split()) > 8:
            consec_prose += 3
    deck = "\n".join(out).rstrip() + "\n"

    sections = set(re.findall(r"\[([A-Z_0-9]+)\]", deck))
    if "OPTIONS" not in sections:
        raise SWMMDeckError(
            "SWMM_DECK_PARSE_FAILED",
            message="extracted block has no [OPTIONS] section",
        )
    logger.info(
        "extract_inline_deck: block=%d bytes=%d sections=%d",
        select_index, len(deck), len(sections),
    )
    return deck


def fetch_deck_text(deck: PublishedDeck, *, timeout: int = 60) -> str:
    """Fetch the pinned public source page and extract the cited deck (runtime).

    Redistribution-honest: the author-posted deck is NOT baked into the repo; it
    is fetched from ``deck.source_url`` on each run and extracted deterministically.
    A network miss OR an extraction miss is a typed ``SWMM_DECK_UNAVAILABLE`` so
    the agent narrates the honest unavailability (never a silent dead-end).
    """
    req = urllib.request.Request(
        deck.source_url,
        headers={"User-Agent": "trid3nt-swmm-deck-runner/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - public page
            page = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        raise SWMMDeckError(
            "SWMM_DECK_UNAVAILABLE",
            message=(
                f"could not fetch the cited deck '{deck.title}' from "
                f"{deck.source_url}: {exc}"
            ),
            details={"deck_id": deck.deck_id, "source_url": deck.source_url},
        ) from exc
    try:
        return extract_inline_deck(page, select_index=deck.select_index)
    except SWMMDeckError as exc:
        # re-tag a parse miss on a reachable page as UNAVAILABLE (the deck we
        # cited is not extractable from the current page render).
        raise SWMMDeckError(
            "SWMM_DECK_UNAVAILABLE",
            message=(
                f"the cited deck '{deck.title}' is not extractable from the current "
                f"page render ({exc}); the published source may have changed"
            ),
            details={"deck_id": deck.deck_id, "source_url": deck.source_url},
        ) from exc


# --------------------------------------------------------------------------- #
# Deterministic parameter overrides (text-edit pattern).
# --------------------------------------------------------------------------- #
def apply_rain_scale(inp_text: str, factor: float) -> tuple[str, str]:
    """Scale every ``[TIMESERIES]`` rainfall value by ``factor`` (deterministic).

    A published hyetograph is the deck's own storm; a rain-scale multiplies each
    time-value pair's VALUE column so the SAME storm profile drives a larger /
    smaller total depth. Only meaningful for a rainfall-forced deck; the caller
    gates on ``deck.rain_scalable``. Returns (edited_text, label). ``factor==1.0``
    is a no-op returning the text unchanged.
    """
    if abs(factor - 1.0) < 1e-9:
        return inp_text, "rain_scale=1.0 (unchanged published storm)"
    out: list[str] = []
    in_ts = False
    n_scaled = 0
    for ln in inp_text.splitlines():
        m = _HDR_RE.match(ln)
        if m:
            in_ts = m.group(1) == "TIMESERIES"
            out.append(ln)
            continue
        if in_ts and ln.strip() and not ln.strip().startswith(";"):
            parts = ln.split()
            # last token is the numeric value; scale it if numeric.
            try:
                val = float(parts[-1])
            except (ValueError, IndexError):
                out.append(ln)
                continue
            parts[-1] = f"{val * factor:g}"
            out.append(" ".join(parts))
            n_scaled += 1
            continue
        out.append(ln)
    label = f"rain_scale={factor:g} (x{n_scaled} hyetograph ordinates)"
    logger.info("apply_rain_scale: factor=%g ordinates=%d", factor, n_scaled)
    return "\n".join(out) + "\n", label


# --------------------------------------------------------------------------- #
# Solve (headless swmm5_run + continuity honesty gate) + result summaries.
# --------------------------------------------------------------------------- #
@dataclass
class DeckSolveResult:
    """The parsed result of solving a published deck.

    Scalars are read from the ``.rpt`` summaries; the raw ``.out``/``.rpt`` paths
    stay on disk for the chart readers. Every field is a real parsed solver
    output (invariant 1).
    """

    inp_path: str
    rpt_path: str
    out_path: str
    continuity_error_pct: float
    n_nodes: int
    n_links: int
    n_subcatchments: int
    peak_outfall_flow_cms: float = 0.0
    total_outfall_volume_m3: float = 0.0
    max_node_depth_m: float = 0.0
    n_flooded_nodes: int = 0
    n_surcharged_conduits: int = 0
    node_max_depth: dict[str, float] = field(default_factory=dict)
    rpt_summary: dict[str, Any] = field(default_factory=dict)


def solve_deck_text(
    inp_text: str,
    *,
    workdir: str | Path | None = None,
    mass_balance_tolerance_pct: float = 10.0,
    stem: str = "deck",
) -> DeckSolveResult:
    """Write ``inp_text`` to disk, solve headless via ``swmm5_run``, gate + parse.

    Reuses the network family's headless one-shot ``swmm5_run`` (swmm-toolkit /
    OWA) - the same solve seam ``run_network_deck`` uses - and applies the
    Flow-Routing-Continuity honesty gate before returning ANY numbers. Raises the
    typed ``SWMMDeckError`` codes on a dependency miss, a solver crash, an
    unreadable continuity, or a continuity over tolerance.
    """
    try:
        from swmm_api import SwmmReport, swmm5_run
    except Exception as exc:  # pragma: no cover - dependency guard
        raise SWMMDeckError(
            "SWMM_DECK_DEPENDENCY_MISSING",
            message=f"swmm-api unavailable for the deck runner: {exc}",
        ) from exc
    from trid3nt_server.agent.mesh.raster_cell_mesh import read_flow_routing_continuity

    base = (
        Path(workdir) if workdir is not None
        else Path(tempfile.mkdtemp(prefix=f"swmm-deck-{stem}-"))
    )
    base.mkdir(parents=True, exist_ok=True)
    inp = str(base / f"{stem}.inp")
    rpt = str(base / f"{stem}.rpt")
    out = str(base / f"{stem}.out")
    Path(inp).write_text(inp_text, encoding="utf-8")

    try:
        # swmm5_run streams a progress bar to stdout; keep it off the agent log.
        with redirect_stdout(io.StringIO()):
            swmm5_run(inp, fn_rpt=rpt, fn_out=out)
    except Exception as exc:  # noqa: BLE001
        raise SWMMDeckError(
            "SWMM_DECK_RUN_FAILED",
            message=f"swmm5_run failed on the published deck: {exc}",
            details={"inp_path": inp},
        ) from exc

    cont = read_flow_routing_continuity(rpt)
    if cont is None:
        raise SWMMDeckError(
            "SWMM_DECK_CONTINUITY_UNREADABLE",
            message="no Flow-Routing Continuity error in the .rpt (run did not complete)",
            details={"rpt_path": rpt},
        )
    if abs(cont) > float(mass_balance_tolerance_pct):
        raise SWMMDeckError(
            "SWMM_MASS_BALANCE_EXCEEDED",
            message=(
                f"Flow-Routing Continuity error {cont:+.3f}% exceeds tolerance "
                f"{mass_balance_tolerance_pct:.1f}% - refusing to publish a "
                f"silently-wrong deck result"
            ),
            details={"continuity_error_pct": cont, "rpt_path": rpt},
        )

    rep = SwmmReport(rpt)
    node_max_depth: dict[str, float] = {}
    max_depth = 0.0
    try:
        nds = rep.node_depth_summary
        if nds is not None:
            for name, row in nds.iterrows():
                d = float(row.get("Maximum_Depth_Meters", 0.0) or 0.0)
                node_max_depth[str(name)] = d
                max_depth = max(max_depth, d)
    except Exception as exc:  # noqa: BLE001
        logger.debug("deck runner: node depth summary unreadable (%s)", exc)

    flooded = 0
    try:
        nfs = rep.node_flooding_summary
        flooded = int(len(nfs)) if nfs is not None else 0
    except Exception:  # noqa: BLE001
        flooded = 0
    surcharged = 0
    try:
        css = rep.conduit_surcharge_summary
        surcharged = int(len(css)) if css is not None else 0
    except Exception:  # noqa: BLE001
        surcharged = 0

    peak_flow = 0.0
    total_vol = 0.0
    try:
        ols = rep.outfall_loading_summary
        if ols is not None and len(ols):
            peak_flow = float(ols["Max_Flow_CMS"].max())
            total_vol = float(ols["Total_Volume_10^6 ltr"].sum()) * 1_000.0
    except Exception as exc:  # noqa: BLE001
        logger.debug("deck runner: outfall loading summary unreadable (%s)", exc)

    n_nodes, n_links, n_subs = _count_objects(inp_text)
    logger.info(
        "solve_deck_text: continuity=%+.3f%% nodes=%d links=%d peak_outfall=%.4g CMS "
        "max_depth=%.3f flooded=%d surcharged=%d",
        cont, n_nodes, n_links, peak_flow, max_depth, flooded, surcharged,
    )
    return DeckSolveResult(
        inp_path=inp, rpt_path=rpt, out_path=out,
        continuity_error_pct=cont, n_nodes=n_nodes, n_links=n_links,
        n_subcatchments=n_subs, peak_outfall_flow_cms=peak_flow,
        total_outfall_volume_m3=total_vol, max_node_depth_m=max_depth,
        n_flooded_nodes=flooded, n_surcharged_conduits=surcharged,
        node_max_depth=node_max_depth,
    )


def _count_objects(inp_text: str) -> tuple[int, int, int]:
    """Count nodes / links / subcatchments from the deck sections (provenance)."""
    node_secs = {"JUNCTIONS", "OUTFALLS", "STORAGE", "DIVIDERS"}
    link_secs = {"CONDUITS", "PUMPS", "ORIFICES", "WEIRS", "OUTLETS"}
    counts: dict[str, int] = {}
    cur: str | None = None
    for ln in inp_text.splitlines():
        m = _HDR_RE.match(ln)
        if m:
            cur = m.group(1)
            continue
        s = ln.strip()
        if cur and s and not s.startswith(";"):
            counts[cur] = counts.get(cur, 0) + 1
    n_nodes = sum(counts.get(s, 0) for s in node_secs)
    n_links = sum(counts.get(s, 0) for s in link_secs)
    n_subs = counts.get("SUBCATCHMENTS", 0)
    return n_nodes, n_links, n_subs


# --------------------------------------------------------------------------- #
# Time-series readers (chart feedstock) - from the solved .out via swmm-api.
# --------------------------------------------------------------------------- #
def _minutes_series(pairs: Any) -> list[tuple[float, float]]:
    """Convert a ``{datetime: value}`` series to ``[(minute_from_start, value)]``."""
    items = list(pairs.items()) if hasattr(pairs, "items") else list(pairs)
    if not items:
        return []
    t0 = items[0][0]
    rows: list[tuple[float, float]] = []
    for ts, v in items:
        try:
            mins = (ts - t0).total_seconds() / 60.0
        except Exception:  # noqa: BLE001
            mins = 0.0
        try:
            rows.append((float(mins), float(v)))
        except (TypeError, ValueError):
            continue
    return rows


def node_depth_series(out_path: str, node: str) -> list[tuple[float, float]]:
    """Depth-vs-minutes series for a node (wet-well / pond stage tracking)."""
    from swmm_api import SwmmOutput

    with SwmmOutput(out_path) as out:
        try:
            ser = out.get_part("node", node, "depth")
        except Exception as exc:  # noqa: BLE001
            logger.debug("node_depth_series(%s): %s", node, exc)
            return []
    return _minutes_series(ser)


def link_flow_series(out_path: str, link: str) -> list[tuple[float, float]]:
    """Flow-vs-minutes series for a link (pump / weir / conduit hydrograph)."""
    from swmm_api import SwmmOutput

    with SwmmOutput(out_path) as out:
        try:
            ser = out.get_part("link", link, "flow")
        except Exception as exc:  # noqa: BLE001
            logger.debug("link_flow_series(%s): %s", link, exc)
            return []
    return _minutes_series(ser)


def subcatchment_runoff_series(out_path: str, sub: str) -> list[tuple[float, float]]:
    """Runoff-vs-minutes series for a subcatchment (LID with/without comparison)."""
    from swmm_api import SwmmOutput

    with SwmmOutput(out_path) as out:
        try:
            ser = out.get_part("subcatchment", sub, "runoff")
        except Exception as exc:  # noqa: BLE001
            logger.debug("subcatchment_runoff_series(%s): %s", sub, exc)
            return []
    return _minutes_series(ser)


def list_object_names(inp_text: str, section: str) -> list[str]:
    """First-token names in a deck section (e.g. STORAGE pond names, PUMPS ids)."""
    names: list[str] = []
    cur: str | None = None
    for ln in inp_text.splitlines():
        m = _HDR_RE.match(ln)
        if m:
            cur = m.group(1)
            continue
        s = ln.strip()
        if cur == section and s and not s.startswith(";"):
            names.append(s.split()[0])
    return names
