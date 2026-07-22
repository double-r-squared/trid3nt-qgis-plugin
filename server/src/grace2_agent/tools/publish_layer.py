"""Atomic tool ``publish_layer`` - raster publish bridge (raw ``s3://`` COG).

This module registers one atomic tool that closes the produce->render loop:

    ``publish_layer(layer_uri, layer_id, style_preset, ...)``
      -> ``str`` (the raster's raw ``s3://`` COG URI, ready for the envelope)

**Live path (s3 + QGIS-native rendering; the only publish path)**

Rasters live as COGs at ``s3://<bucket>/<key>`` on the object store (MinIO
locally). The QGIS plugin - the ONLY client - opens the COG DIRECTLY via
GDAL ``/vsicurl/`` (the same s3->http translation it already uses for
FlatGeobuf vectors) and applies its own renderer from the envelope's
legend/style fields, so the publish emits the raw ``s3://`` URI itself:

1. Guard against unresolved layer handles / placeholder URIs (typed,
   retryable errors that name the case's real handles).
2. Vectors: benign no-op (they already render inline via their producing
   fetch tool's GeoJSON), OR a durable per-Case GeoJSON asset (#165 P0),
   OR - when ``GRACE2_QGIS_WMS_BASE`` is exported - a styled QGIS Server
   WMS GetMap face.
3. Rasters: enforce COG overviews (F33; auto-translate when missing),
   resolve styling via ``_resolve_titiler_style_params`` (F51 - THE render
   chokepoint: categorical/RGBA/terrain passthroughs, then the typed preset
   registry, then band-stats percentile fallback, then a safe default;
   the resolver math is UNCHANGED - only its output destination moved from
   a tile-URL query string into the stashed legend), stash the data-driven
   legend keyed by the ``s3://`` uri the envelope will carry, and register
   the layer via ``observe_published_layer``.

TiTiler EXIT (2026-07): the tool no longer mints
``{tile_base}/cog/tiles/WebMercatorQuad/{z}/{x}/{y}`` XYZ templates and no
longer reads ``GRACE2_TILE_SERVER_BASE``. Old persisted cases still carry
legacy tile-template URIs; a re-publish of one is UNWRAPPED to its embedded
``url=`` s3 COG (the ``export_case_to_qgis._unwrap_tile_template`` trick)
and flows through the normal raster path, and the plugin unwraps legacy
templates it rehydrates on its own. No worker round-trip, no ``.qgs``
mutation on the raster path.

The GCP-era publish path (the legacy cloud QGIS-worker dispatch,
``gs://``/``/vsigs/`` staging, GCS ``.qgs`` verification) was removed with
the cloud strip - ``cache.storage_scheme()`` is pinned to ``"s3"``.

**Cross-cutting principles:**

- **FR-DC-6 (uncacheable enumeration): preserves.** Side-effect tool;
  ``cacheable=False``, ``ttl_class="live-no-cache"``,
  ``source_class="publish_layer"``.
- **NFR-R-1 (resilience):** failures surface as typed ``PublishLayerError``
  (not unhandled exceptions); style/legend/overview probes fail OPEN so a
  publish is never blocked by a best-effort enhancement.
"""

from __future__ import annotations

import logging
import os
import tempfile
from typing import Any

from grace2_contracts import new_ulid
from grace2_contracts.tool_registry import AtomicToolMetadata

from ..uri_registry import observe_published_layer
from . import register_tool

__all__ = [
    "publish_layer",
    "PublishLayerError",
    "derive_layer_id",
    "derive_readable_layer_name",
    "style_params_from_band_stats",
    "legend_for_published_layer",
    "pop_legend_for_uri",
    "set_default_qgs_uri",
    "DEFAULT_PROJECT_QGS_URI",
]

logger = logging.getLogger("grace2_agent.tools.publish_layer")


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

#: Default canonical project .qgs URI (heritage default; consumed only by the
#: ``GRACE2_QGIS_WMS_BASE`` vector-WMS seam and ``case_lifecycle``'s template
#: resolution - override via ``GRACE2_CASE_QGS_TEMPLATE`` / ``set_default_qgs_uri``).
DEFAULT_PROJECT_QGS_URI: str = "s3://trid3nt-qgs/sample.qgs"


# --------------------------------------------------------------------------- #
# Error class
# --------------------------------------------------------------------------- #


class PublishLayerError(RuntimeError):
    """Raised when ``publish_layer`` cannot complete the round-trip.

    The ``error_code`` attribute carries a SCREAMING_SNAKE_CASE code so the
    agent surface can render a useful failure narration and the pipeline strip
    shows ``UPSTREAM_API_ERROR``. ``retryable`` (job-0177 contract; harvested
    by ``adapter._classify_error``) tells Gemini whether re-issuing the call
    with corrected args can succeed.

    Codes:
    - ``QGS_URI_PARSE_ERROR`` - malformed ``project_qgs_uri`` (vector-WMS seam).
    - ``UNKNOWN_LAYER_HANDLE`` (2026-07-13, retryable) - ``layer_uri`` is a
      bare placeholder token or fabricated scheme that no registry entry
      resolved; the message names the case's available handles so the model
      retries with one verbatim (OPEN-17 small-model class).
    - ``LAYER_URI_NOT_FOUND`` (retryable) - ``layer_uri`` is not an ``s3://``
      COG on this deployment; the model should re-issue with the producing
      tool's layer handle or its ``s3://`` URI verbatim.
    """

    def __init__(self, error_code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable


# --------------------------------------------------------------------------- #
# DI seams
# --------------------------------------------------------------------------- #

_DEFAULT_QGS_URI: str | None = None


def set_default_qgs_uri(uri: str | None) -> None:
    """Override the default canonical .qgs URI.

    Useful for smoke harnesses and integration tests that target a non-production
    project. ``None`` restores the constant default.
    """
    global _DEFAULT_QGS_URI
    _DEFAULT_QGS_URI = uri


def _get_effective_qgs_uri(project_qgs_uri: str | None) -> str:
    if project_qgs_uri is not None:
        return project_qgs_uri
    if _DEFAULT_QGS_URI is not None:
        return _DEFAULT_QGS_URI
    return DEFAULT_PROJECT_QGS_URI


def _parse_qgs_key(qgs_uri: str) -> str:
    """Extract the object key (no leading slash) from a gs:// or s3:// URI.

    Used to build the MAP= parameter in the WMS URL. Both schemes share the
    ``<scheme>://<bucket>/<key>`` shape, so the key extraction is identical.
    On the GCP path the .qgs lives at ``gs://...``; on AWS (job-0308) it lives
    at ``s3://...``; the AWS QGIS-vector WMS branch (GRACE2_QGIS_WMS_BASE set)
    must accept the s3:// form or the branch fails.

    Examples:
        ``s3://trid3nt-qgs/sample.qgs`` -> ``sample.qgs``
        ``gs://legacy-cloud-qgs/sample.qgs`` -> ``sample.qgs``

    Raises:
        PublishLayerError: if the URI is not a gs:// or s3:// URI, or has no
        key component.
    """
    for scheme in ("gs://", "s3://"):
        if qgs_uri.startswith(scheme):
            rest = qgs_uri[len(scheme):]
            break
    else:
        raise PublishLayerError(
            "QGS_URI_PARSE_ERROR",
            f"project_qgs_uri must be a gs:// or s3:// URI; got {qgs_uri!r}",
        )
    # <scheme>://<bucket>/<key>
    slash_idx = rest.find("/")
    if slash_idx == -1 or slash_idx == len(rest) - 1:
        raise PublishLayerError(
            "QGS_URI_PARSE_ERROR",
            f"project_qgs_uri has no key component: {qgs_uri!r}",
        )
    key = rest[slash_idx + 1:]
    return key


#: Env var that, WHEN SET, activates the s3-branch QGIS-vector publish route.
#: It is the base URL of the AWS QGIS Server WMS endpoint (e.g.
#: ``https://<cloudfront>/ogc/wms``). The route lands ahead of the AWS QGIS
#: infra (sprint-16 job-0308): until ``GRACE2_QGIS_WMS_BASE`` is exported the
#: s3 branch keeps the existing ``_benign_vector_noop`` (vectors already render
#: inline via their producing fetch tool's GeoJSON), so LIVE behavior is
#: UNCHANGED. Once the QGIS Server is stood up, exporting this var flips
#: publish_layer to compose a styled WMS GetMap face for the vector.
_QGIS_WMS_BASE_ENV: str = "GRACE2_QGIS_WMS_BASE"


def _get_qgis_wms_base() -> str:
    """Return the configured AWS QGIS Server WMS base (trailing slash stripped).

    Empty string when ``GRACE2_QGIS_WMS_BASE`` is unset/blank - the caller
    treats that as "infra not yet stood up" and falls back to the benign no-op.
    """
    return os.environ.get(_QGIS_WMS_BASE_ENV, "").rstrip("/")


def _build_vector_wms_url(
    wms_base: str,
    layer_uri: str,
    layer_id: str,
    qgs_key: str,
) -> str:
    """Compose a styled WMS GetMap URL for a VECTOR on the AWS QGIS path.

    Mirrors the GCP ``_build_wms_url`` shape (``MAP=<.qgs key>&LAYERS=<id>``)
    but points at ``GRACE2_QGIS_WMS_BASE`` (the AWS QGIS Server) and carries
    the standard WMS GetMap envelope so ``uri_registry._looks_like_wms``
    recognizes it as a renderable display face. The MAP= param uses the same
    ``/mnt/qgs/<key>`` mount convention as the GCP worker path.

    Style seam: the family-aware ``_infer_style_preset`` (the same selector the
    raster paths use) is threaded into a ``STYLES=`` value so the QGIS Server
    can apply a named style when one is registered; the empty-string default
    (terrain-family / unknown) yields ``STYLES=`` which is a valid WMS "server
    default style" request.
    """
    from urllib.parse import quote

    style = _infer_style_preset(layer_uri, layer_id)
    map_param = f"/mnt/qgs/{qgs_key}"
    return (
        f"{wms_base}?MAP={quote(map_param, safe='/')}"
        "&SERVICE=WMS&VERSION=1.3.0&REQUEST=GetMap"
        f"&LAYERS={quote(layer_id, safe='')}"
        f"&STYLES={quote(style, safe='')}"
        "&FORMAT=image/png&TRANSPARENT=true"
    )


#: job-0269b: token vocabulary marking TERRAIN-family rasters. These are
#: RGBA (colored relief) or single-band grayscale/Float32 (hillshade, slope,
#: aspect, raw DEM) products - QGIS DEFAULT rendering visualizes them
#: correctly, while the flood-depth pseudocolor ramp clamps them to a
#: uniform/transparent tile (live 2026-06-10 "can't see the overlay").
#: Token-boundary matching (not substring) so e.g. a layer_id like
#: ``"demo-flood"`` does NOT match ``dem``.
_TERRAIN_STYLE_TOKENS = frozenset(
    # slope/aspect REMOVED 2026-06-24 (tools-backlog #3): they now carry real
    # colormaps (slope_angle_deg ylorrd / aspect_compass_deg hsv) via the style
    # registry, routed by _infer_style_preset below. dem/relief/hillshade/terrain/
    # elevation STAY grayscale -- bare DEM + shaded relief render correctly unstyled.
    {"dem", "relief", "hillshade", "terrain", "elevation"}
)

#: tools-backlog #3 -- URI/id token -> the slope/aspect colormap preset, applied
#: BEFORE the terrain passthrough so an auto-inferred slope/aspect layer is
#: colormapped (not left grayscale and not mis-defaulted to flood depth).
_SLOPE_ASPECT_PRESET_BY_TOKEN: dict[str, str] = {
    "slope": "slope_angle_deg",
    "aspect": "aspect_compass_deg",
}


def _infer_style_preset(layer_uri: str, layer_id: str) -> str:
    """Family-aware default style preset (job-0269b).

    Returns the slope/aspect colormap preset for those families (tools-backlog
    #3), ``""`` (no preset → QGIS default rendering) for the remaining terrain
    rasters (dem/relief/hillshade/terrain/elevation), else
    ``"continuous_flood_depth"``  --  the pre-0269b default, so flood/plume
    publishes that relied on it are unchanged. Tokenizes BOTH the resolved URI
    and the layer_id on non-alphanumerics and matches    whole tokens against ``_TERRAIN_STYLE_TOKENS``.
    """
    import re as _re

    tokens = set(
        _re.split(r"[^a-z0-9]+", f"{layer_uri} {layer_id}".lower())
    )
    for token, preset in _SLOPE_ASPECT_PRESET_BY_TOKEN.items():
        if token in tokens:
            return preset
    if tokens & _TERRAIN_STYLE_TOKENS:
        return ""
    return "continuous_flood_depth"


# --------------------------------------------------------------------------- #
# F51: TiTiler style resolver (AWS s3 branch)
#
# On the AWS deployment rasters publish through TiTiler, which reads the COG
# directly and renders a single-band float32 raster as PER-TILE-AUTOSCALED
# GRAYSCALE unless the tile request carries an explicit ``&rescale=<lo>,<hi>``
# and ``&colormap_name=<name>``. Before F51 only ``continuous_flood_depth`` and
# ``continuous_plume_concentration`` got params (a 2-entry if/elif) - every
# OTHER continuous preset (precip / temperature / wind / drought / fuel
# moisture / satellite) fell through to ``style_params=""`` and rendered
# invisible / washed-out.
#
# ``_resolve_titiler_style_params`` is the single resolution point. CRITICAL
# guards run FIRST so rasters that are ALREADY colorized are never corrupted by
# a single-band rescale/colormap (the HIGH-severity terrain/RGBA regression a
# rescale would otherwise introduce):
#   - categorical / paletted COG (NLCD land cover) -> "" (embedded GDAL color
#     table wins, job-0324);
#   - RGB(A) / multiband COG (colored relief, blended landcover + hillshade
#     composite - NATE's Toutle demo) -> "" (TiTiler renders the baked colors
#     directly);
#   - terrain-token preset/URI (continuous_dem / hillshade / slope / aspect /
#     relief / terrain / elevation) -> "" (grayscale terrain auto-scales, RGBA
#     terrain renders directly - exactly as it did pre-F51).
# Only AFTER those passthroughs does it apply a typed preset->(rescale,colormap)
# REGISTRY (exact key first, then sensible substring/prefix) to SINGLE-BAND
# weather SCALARS, then a GENERIC band-stats percentile fallback for any
# single-band continuous preset not in the registry, then a SAFE non-empty
# default. Colormap names are LOWERCASE rio-tiler names (viridis, blues, ylgnbu,
# reds, rdbu, rdylbu_r, ylgn, ylorrd, gray, gray_r, ...) - rio-tiler casing is
# lowercase (NOT matplotlib), do not change.
# --------------------------------------------------------------------------- #

#: Exact preset / variable key -> (rescale "lo,hi", colormap_name). Physically
#: correct band + colormap per family. KEEP flood/plume byte-for-byte.
_TITILER_STYLE_REGISTRY: dict[str, tuple[str, str]] = {
    # Hydrology (UNCHANGED - pre-F51 behavior pinned by tests).
    "continuous_flood_depth": ("0,3", "ylgnbu"),
    "continuous_plume_concentration": ("0,10", "reds"),
    # SnapWave significant wave height (m) - sprint-17 wave animation. A
    # CYAN/BLUE ramp (gnbu) over 0..6 m, visibly DISTINCT from depth's ylgnbu so
    # the wave layer group never looks identical to the flood-depth group on the
    # Mexico Beach North Star. ADDITIVE - depth/plume stay byte-identical.
    "continuous_wave_height": ("0,6", "gnbu"),
    # Precipitation (mm).
    "precipitation_mm": ("0,100", "blues"),
    "gridmet_pr": ("0,100", "blues"),
    "era5_total_precipitation": ("0,100", "blues"),
    # Temperature (Kelvin) - exact members; *temperature* prefix catches more.
    "hrrr_2m_temperature": ("250,320", "rdylbu_r"),
    "gridmet_tmmx": ("250,320", "rdylbu_r"),
    "gridmet_tmmn": ("250,320", "rdylbu_r"),
    "era5_2m_temperature": ("250,320", "rdylbu_r"),
    # Wind speed (derived scalar, m/s).
    "wind_speed": ("0,25", "viridis"),
    "hrrr_10m_wind_speed": ("0,25", "viridis"),
    "gridmet_vs": ("0,25", "viridis"),
    # Signed wind components (m/s) - diverging ramp centered on 0.
    "hrrr_10m_u_wind": ("-25,25", "rdbu"),
    "hrrr_10m_v_wind": ("-25,25", "rdbu"),
    "era5_10m_u_wind": ("-25,25", "rdbu"),
    "era5_10m_v_wind": ("-25,25", "rdbu"),
    # Drought + fuel moisture.
    "gridmet_pdsi": ("-6,6", "rdbu"),
    "gridmet_fm100": ("0,40", "ylgn"),
    "gridmet_fm1000": ("0,40", "ylgn"),
    # GOES satellite - visible reflectance vs brightness-temperature bands.
    "goes_visible": ("0,1", "gray"),
    "goes_ir": ("180,330", "gray_r"),
    "goes_wv": ("180,330", "gray_r"),
    # sprint-17 NEW engines (parallel lanes) - ADDITIVE; flood/plume/wave above
    # stay byte-identical. River<->aquifer seepage is SIGNED (gaining vs losing
    # reach) -> a diverging rdbu ramp centered on 0; seismic PGA in [0,1] g -> a
    # perceptually-uniform magma ramp; landslide susceptibility/probability in
    # [0,1] -> a red(high)->green(low) rdylgn_r ramp.
    "diverging_river_seepage": ("-100,100", "rdbu"),
    # GAIA sediment bed-evolution (deposition/erosion, mm): a SIGNED field
    # (deposition positive / erosion negative) -> a diverging rdbu ramp centered
    # on 0, same pattern as river seepage. The deposition COG carries a data-
    # driven legend (mm-scale) so the actual range renders; this registry range is
    # the fallback (a fixed mm band would wash out sub-mm event deposition).
    "diverging_bed_evolution": ("-20,20", "rdbu"),
    # sprint-WQ: SWMM per-cell peak washoff CONCENTRATION (mg/L) - a sequential
    # YlOrBr ramp (low->high pollutant load), visibly distinct from depth's
    # SWMM WQ concentration (SWMM-WQ-1 fix 2026-07-21): INTENTIONALLY NOT a fixed
    # entry. Pollutant concentration ranges span orders of magnitude across
    # pollutants AND sites -- TSS is ~0-300 mg/L but E. coli is #/L in the 1e3-1e7
    # range, so a single fixed rescale saturates one of them. The
    # ``continuous_concentration`` preset therefore falls through to the GENERIC
    # p2/p98 PERCENTILE fallback below, which auto-scales EACH pollutant COG to
    # its OWN data range (viridis ramp). Do not re-add a fixed rescale here.
    "continuous_seismic_pga": ("0,1", "magma"),
    "continuous_landslide_susceptibility": ("0,1", "rdylgn_r"),
    # conservation micro-North-Star -- ADDITIVE. NDVI is the canonical
    # vegetation index in [-1, 1]; bare/water near 0, healthy canopy ~0.6-0.9 ->
    # a green-up rdylgn ramp rescaled to the full physical range. MoBI
    # imperiled-species importance is strictly positive (low->high); a ylgn ramp
    # over a typical richness band reads as a biodiversity hotspot map.
    # (NAIP RGB is a multiband COG -- handled by the RGBA/multiband passthrough
    # in _resolve_titiler_style_params, NOT a single-band registry entry, so
    # "naip_rgb" is intentionally absent here.)
    "ndvi": ("-1,1", "rdylgn"),
    "mobi_biodiversity": ("0,40", "ylgn"),
    # canopy-height ML-inference tool (Meta HighResCanopyHeight on CPU Batch).
    # ESTIMATED canopy top height in METRES (a single-band float32 COG); typical
    # forest canopies are ~0..40 m, so a 0..40 m greens ramp (rio-tiler "greens")
    # reads as a height map (bare/low near 0 -> tall canopy at the top of the
    # ramp). ADDITIVE -- the entries above stay byte-identical.
    "canopy_height_m": ("0,40", "greens"),
    # ----------------------------------------------------------------------- #
    # engine-coverage-levers STEP 3 -- NEW published output quantities.
    # ADDITIVE; every entry above stays byte-identical. A SPEC.style_preset that
    # is NOT in this registry silently falls through to a percentile rescale
    # (a physically-wrong colormap), so a CI guard
    # (test_output_quantity_style_presets_resolve) asserts every engine
    # OUTPUT_QUANTITIES style_preset resolves HERE.
    #
    # MODFLOW head / water-table (m, local datum). A continuous head surface
    # rendered with a perceptually-uniform viridis ramp over a generous head
    # band so the gradient reads as a potentiometric surface. (The plume
    # timeseries reuses continuous_plume_concentration above -- not a new key.)
    "continuous_head_m": ("0,50", "viridis"),
    # MODFLOW archetype products (sprint-18 Wave-1/Wave-2): distinct semantic ramps
    # so drawdown (water DECLINE) and mounding (water RISE) never render with the
    # same colormap. Registered so the OUTPUT_QUANTITIES style_preset specs validate.
    "continuous_drawdown_m": ("0,10", "reds"),  # head decline under pumping
    "continuous_dewatering_rate": ("0,5000", "reds"),  # DRN outflow (m3/day)
    "continuous_mounding_m": ("0,10", "blues"),  # head rise under recharge (MAR)
    # CSUB land subsidence (sprint sim-addons): ground compaction in cm, positive
    # DOWN (subsidence). A sequential hot ramp so a deep bowl reads as intense; a
    # subsidence run only produces positive values (pumping compaction), so a
    # 0-based range is correct. Registered so the SubsidenceLayerURI style_preset
    # validates instead of silently falling back to the percentile default.
    "continuous_subsidence_cm": ("0,50", "inferno"),  # ground compaction (cm, +down)
    "continuous_hydroperiod_m": ("0,5", "viridis"),  # seasonal water-table range
    # Landlab discarded fields the component chain already computes. Drainage
    # area spans many orders of magnitude -> a high-contrast viridis (the
    # percentile fallback would also work, but pinning a key keeps the colormap
    # stable across runs); slope is a 0..1 rise/run gradient -> a ylorrd "steep
    # = hot" ramp; relative wetness in [0,1] -> a blues "wetter = darker" ramp;
    # overland discharge (m^3/s) -> the same blues family as wetness but a wider
    # band; the deterministic factor-of-safety field is dimensionless with
    # FoS<1 = failure -> a rdylgn ramp (low/red = unstable, high/green = stable)
    # rescaled 0..2 so FoS=1 sits at the diverging midpoint.
    "continuous_drainage_area": ("0,1000000", "viridis"),
    "continuous_slope": ("0,1", "ylorrd"),
    "continuous_relative_wetness": ("0,1", "blues"),
    "continuous_discharge_m3s": ("0,50", "blues"),
    "continuous_factor_of_safety": ("0,2", "rdylgn"),
    # SWMM additional node/link outputs the Output API already exposes.
    # Node FLOODING_LOSSES (surface flooding rate, cfs/cms) and PONDED_VOLUME
    # (ponded water volume) read as "how much water is ponding / where does it
    # surcharge" -> a blues ramp; conduit FLOW_RATE (signed, m^3/s) -> a
    # diverging rdbu centered on 0 (direction-aware); conduit FLOW_VELOCITY
    # (m/s) -> a viridis speed ramp.
    "continuous_flooding_losses": ("0,5", "blues"),
    "continuous_ponded_volume": ("0,1000", "blues"),
    "diverging_conduit_flow": ("-10,10", "rdbu"),
    "continuous_conduit_velocity": ("0,5", "viridis"),
    # tools-backlog #3 -- per-tool colormaps replacing the generic continuous_dem
    # placeholder. ADDITIVE; entries above stay byte-identical. Impervious surface
    # is 0..100 percent -> a reds "more paved = redder" ramp; population is
    # people-per-pixel (WorldPop ~100 m / ACS) -> a magma density ramp. Slope ANGLE
    # in DEGREES (0..90; most terrain <60) -> a "steep = hot" ylorrd ramp; aspect is
    # a COMPASS direction (0..360) -> the cyclic hsv ramp so North reads the same
    # hue at 0 and 360. To reach these, "slope"/"aspect" were removed from
    # _TERRAIN_STYLE_TOKENS (the terrain passthrough now keeps ONLY dem/relief/
    # hillshade/terrain/elevation grayscale; hillshade SHOULD stay grayscale as
    # shaded relief). NATE 2026-06-24: the backend colormaps land HERE; the
    # Orchestrator finishes by wiring the frontend legends + substrate.
    "impervious_surface_pct": ("0,100", "reds"),
    "population_density": ("0,250", "magma"),
    "slope_angle_deg": ("0,60", "ylorrd"),
    "aspect_compass_deg": ("0,360", "hsv"),
    # FIRE-3 (ELMFIRE wildfire spread) -- ADDITIVE; entries above stay
    # byte-identical. Time-of-arrival is HOURS from ignition over a typical
    # scenario window (<= 24 h band; early arrival = dark, the advancing front
    # = bright) -> the perceptually-uniform inferno "fire" ramp; flame length
    # in METRES (postprocess converts ELMFIRE's feet once; most surface fires
    # < 10 m) -> a "longer = hotter" ylorrd ramp; spread rate in m/min
    # (ELMFIRE ft/min converted once; head-fire rates are typically < 30
    # m/min) -> an oranges intensity ramp, visibly distinct from flame length.
    "continuous_fire_arrival_hr": ("0,24", "inferno"),
    "continuous_flame_length_m": ("0,10", "ylorrd"),
    "continuous_fire_spread_rate": ("0,30", "oranges"),
}

#: Safe non-empty default - never let a continuous raster fall through to an
#: empty ``style_params`` (which gives stock per-tile grayscale autoscale).
_TITILER_SAFE_DEFAULT = "&rescale=0,1&colormap_name=viridis"


def _sediment_yield_log_style_params() -> str:
    """LOG-SCALED interval ``&colormap=`` for ``sediment_yield_t_ha_yr``.

    RUSLE annual soil loss spans orders of magnitude (0.01 .. 1000+ t/ha/yr),
    so a linear ``&rescale`` would paint everything below the worst gullies as
    one flat color. Instead we emit a TiTiler/rio-tiler INTERVAL colormap
    (``[[[min, max], [r, g, b, a]], ...]``) whose class breaks are the
    log-spaced 1/5/10/50/100/500 t/ha/yr table owned by
    ``compute_sediment_yield.SEDIMENT_YIELD_LOG_CLASSES`` (single source of
    truth -- the tool builds its LayerURI ``legend`` from the SAME table, so
    the key always matches the paint). Lazy import mirrors the
    ``_published_scenario_tool_names`` pattern (no import-order coupling).
    ADDITIVE: every existing ``&rescale=..&colormap_name=..`` entry is
    byte-identical.
    """
    import json as _json
    from urllib.parse import quote

    from .compute_sediment_yield import SEDIMENT_YIELD_LOG_CLASSES, hex_to_rgba

    intervals = [
        [[lo, hi], hex_to_rgba(color)]
        for lo, hi, color, _label in SEDIMENT_YIELD_LOG_CLASSES
    ]
    return "&colormap=" + quote(_json.dumps(intervals), safe="")


def _registry_style_params(preset: str) -> str | None:
    """Return ``&rescale=..&colormap_name=..`` for a known preset, else ``None``.

    Exact key first, then sensible substring/prefix matching so future variants
    (e.g. ``era5_2m_temperature_max``, ``hrrr_2m_temperature_anomaly``) still
    land in the right physical band. ``hrrr_smoke_*`` is intentionally EXCLUDED
    here (its range is ~1e-9..1e-6) so it falls through to the band-stats
    generic auto-rescale below.
    """
    key = (preset or "").lower()
    if not key:
        return None
    # 0. RUSLE soil loss -> LOG-SCALED interval colormap (t/ha/yr spans orders
    #    of magnitude; see _sediment_yield_log_style_params).
    if key == "sediment_yield_t_ha_yr":
        return _sediment_yield_log_style_params()
    # 1. Exact match.
    hit = _TITILER_STYLE_REGISTRY.get(key)
    if hit is not None:
        rescale, cmap = hit
        return f"&rescale={rescale}&colormap_name={cmap}"
    # 2. hrrr_smoke_* -> generic band-stats fallback (tiny range).
    if "smoke" in key:
        return None
    # 3. Family substring/prefix matching (order matters - most specific first).
    #    (substring, (rescale, colormap))
    family_rules: tuple[tuple[str, tuple[str, str]], ...] = (
        # Signed wind components before generic "wind"/"temperature".
        ("u_wind", ("-25,25", "rdbu")),
        ("v_wind", ("-25,25", "rdbu")),
        ("wind_speed", ("0,25", "viridis")),
        ("temperature", ("250,320", "rdylbu_r")),
        ("pdsi", ("-6,6", "rdbu")),
        ("fm100", ("0,40", "ylgn")),
        ("fm1000", ("0,40", "ylgn")),
    )
    for needle, (rescale, cmap) in family_rules:
        if needle in key:
            return f"&rescale={rescale}&colormap_name={cmap}"
    # Precipitation family - PRECISE match, NOT a loose ``precip`` substring,
    # so ``precipitable_water`` (and other ``precip*`` look-alikes) do NOT get
    # the 0,100 mm precip ramp. Exact keys are already handled above; here we
    # accept only a guarded prefix on the conventional precip variable names.
    if (
        key.endswith("_precip")
        or key.endswith("_precipitation")
        or key.endswith("precipitation_mm")
        or key.endswith("_pr")
        or "_precipitation_" in key
    ):
        return "&rescale=0,100&colormap_name=blues"
    return None


def _band1_percentile_rescale(raster_bytes: bytes | None) -> str | None:
    """Compute ``&rescale=<p2>,<p98>&colormap_name=viridis`` from band-1 stats.

    Reads band 1 from the in-hand COG bytes via a rasterio ``MemoryFile``,
    masks nodata + non-finite values, and emits the 2nd/98th percentile rescale
    with a perceptually-uniform ``viridis`` ramp. Returns ``None`` when the
    bytes are missing, unreadable, or band 1 has NO finite values - callers
    degrade to the SAFE default. Single-value / tiny-range bands are widened so
    ``rescale`` is never a zero-width interval (which TiTiler rejects).
    """
    if not raster_bytes:
        return None
    try:
        import numpy as np
        import rasterio
        from rasterio.io import MemoryFile
    except Exception as exc:  # noqa: BLE001 - deps unavailable: safe-default
        logger.debug("band-stats deps unavailable (%s: %s)", type(exc).__name__, exc)
        return None
    try:
        with MemoryFile(raster_bytes) as mem, mem.open() as src:
            band = src.read(1, masked=True)
            arr = np.ma.filled(band.astype("float64"), np.nan)
            finite = arr[np.isfinite(arr)]
            if finite.size == 0:
                return None
            lo = float(np.percentile(finite, 2))
            hi = float(np.percentile(finite, 98))
    except Exception as exc:  # noqa: BLE001 - unreadable / not a raster
        logger.debug(
            "band-stats read failed (%s: %s)", type(exc).__name__, exc
        )
        return None
    if not (lo == lo and hi == hi):  # NaN guard (paranoia)
        return None
    if hi <= lo:
        # Single-value / zero-width: widen around the value so TiTiler accepts
        # a non-degenerate range. Use a relative pad, with an absolute floor.
        pad = max(abs(lo) * 0.01, 1e-6)
        lo, hi = lo - pad, hi + pad
    return f"&rescale={lo:g},{hi:g}&colormap_name=viridis"


def _is_rgba_or_multiband(raster_bytes: bytes | None) -> bool:
    """True if the COG is RGB(A)/multiband - TiTiler renders it DIRECTLY.

    Reads the in-hand COG bytes via a rasterio ``MemoryFile`` and reports True
    when band count >= 3 OR any band's color interpretation is one of
    Red/Green/Blue/Alpha. Such rasters (colored relief, blended landcover +
    hillshade composites) are already colorized: a single-band ``&rescale`` +
    ``&colormap_name`` would corrupt them, so the resolver returns ``""`` (empty
    style_params = TiTiler passthrough) for them, exactly as the pre-F51 path
    did. Best-effort: returns False on any read failure so a real single-band
    scalar still gets its rescale.
    """
    if not raster_bytes:
        return False
    try:
        import rasterio
        from rasterio.enums import ColorInterp
        from rasterio.io import MemoryFile
    except Exception as exc:  # noqa: BLE001 - deps unavailable: not RGBA
        logger.debug("rgba probe deps unavailable (%s: %s)", type(exc).__name__, exc)
        return False
    try:
        with MemoryFile(raster_bytes) as mem, mem.open() as src:
            if src.count >= 3:
                return True
            rgba = {
                ColorInterp.red,
                ColorInterp.green,
                ColorInterp.blue,
                ColorInterp.alpha,
            }
            return any(ci in rgba for ci in src.colorinterp)
    except Exception as exc:  # noqa: BLE001 - unreadable / not a raster
        logger.debug("rgba probe read failed (%s: %s)", type(exc).__name__, exc)
        return False


def _is_terrain_token_preset(style_preset: str | None, layer_uri: str) -> bool:
    """True if the preset / URI tokenizes to a TERRAIN-family token.

    Reuses ``_TERRAIN_STYLE_TOKENS`` (dem, relief, hillshade, slope, aspect,
    terrain, elevation). Tokenizes the ``style_preset`` AND ``layer_uri`` on
    non-alphanumerics and matches whole tokens, so e.g. ``"continuous_dem"``
    tokenizes to ``{continuous, dem}`` -> matches ``dem``. Terrain rasters
    (grayscale hillshade/slope/aspect, RGBA colored relief) render correctly
    with NO rescale (the pre-F51 behavior), so the resolver returns ``""`` for
    them before trying the registry / band-stats.
    """
    import re as _re

    tokens = set(
        _re.split(r"[^a-z0-9]+", f"{style_preset or ''} {layer_uri or ''}".lower())
    )
    return bool(tokens & _TERRAIN_STYLE_TOKENS)


def _resolve_titiler_style_params(
    style_preset: str | None, layer_uri: str
) -> str:
    """Resolve TiTiler ``&rescale=..&colormap_name=..`` for the s3 publish path.

    Resolution order (F51, hardened by the terrain/RGBA regression fix):

    1. CATEGORICAL GUARD - if the COG carries an embedded band-1 GDAL color
       table (NLCD land cover etc.), return ``""`` so TiTiler colorizes from the
       EMBEDDED palette and is NEVER washed out by a rescale (job-0324).
    2. RGBA / MULTIBAND PASSTHROUGH - if the COG is RGB(A) / >=3 bands (colored
       relief, blended landcover + hillshade composite), return ``""``: TiTiler
       renders the baked colors directly; a single-band rescale/colormap would
       CORRUPT it. This covers NATE's Toutle landcover+hillshade composite
       regardless of preset.
    3. TERRAIN-TOKEN PASSTHROUGH - if the preset / URI tokenizes to a terrain
       token (``continuous_dem`` -> ``dem``, hillshade / slope / aspect / relief
       / terrain / elevation), return ``""``: grayscale terrain auto-scales and
       RGBA terrain renders directly, exactly as it did pre-F51.
    4. REGISTRY - a typed preset/variable -> (rescale, colormap) lookup (exact
       key, then family substring/prefix). Flood + plume are pinned here
       byte-for-byte; single-band weather scalars (precip / temperature / wind /
       drought / fuel-moisture / satellite) get their physically-correct band.
    5. GENERIC FALLBACK - for any single-band continuous preset NOT in the
       registry (and for ``hrrr_smoke_*``), compute the band-1 2nd/98th
       percentile rescale with a viridis ramp from the in-hand COG bytes.
    6. SAFE DEFAULT - if the stats read fails for ANY reason, emit
       ``&rescale=0,1&colormap_name=viridis``. NEVER returns empty for a
       single-band continuous scalar raster.

    The COG bytes are read ONCE here and reused for the categorical-palette
    probe, the RGBA/multiband probe, and the percentile fallback (no double
    download).
    """
    raster_bytes = _read_raster_bytes(layer_uri)

    # 1. Categorical / paletted raster -> NO rescale (embedded palette wins).
    if raster_bytes is not None:
        try:
            import rasterio
            from rasterio.io import MemoryFile

            with MemoryFile(raster_bytes) as mem, mem.open() as src:
                if _read_band1_colormap(src) is not None:
                    logger.info(
                        "publish_layer (titiler) %s carries an embedded band-1 "
                        "color table - leaving style_params empty so TiTiler "
                        "colorizes from the palette (job-0324)",
                        layer_uri,
                    )
                    return ""
        except Exception as exc:  # noqa: BLE001 - palette probe is best-effort
            logger.debug(
                "palette probe skipped (%s: %s)", type(exc).__name__, exc
            )

    # 2. RGBA / multiband composite -> NO rescale (TiTiler renders directly).
    #    Colored relief + blended landcover/hillshade composites are already
    #    colorized; a single-band rescale/colormap would corrupt them. Pre-F51
    #    these published with EMPTY style_params and rendered correctly.
    if _is_rgba_or_multiband(raster_bytes):
        logger.info(
            "publish_layer (titiler) %s is RGB(A)/multiband - leaving "
            "style_params empty so TiTiler renders the baked colors directly "
            "(no single-band rescale/colormap)",
            layer_uri,
        )
        return ""

    # 3. Terrain-family preset/URI -> NO rescale. Grayscale hillshade/slope/
    #    aspect auto-scales and RGBA colored relief renders directly, as it did
    #    pre-F51. ``continuous_dem`` tokenizes to include ``dem`` -> matches.
    if _is_terrain_token_preset(style_preset, layer_uri):
        logger.info(
            "publish_layer (titiler) preset=%r uri=%s is a TERRAIN-family raster "
            "- leaving style_params empty (grayscale/RGBA terrain renders "
            "correctly with no rescale)",
            style_preset,
            layer_uri,
        )
        return ""

    # 4. Typed registry (exact + family). Flood/plume + single-band weather
    #    scalars pinned here.
    params = _registry_style_params(style_preset or "")
    if params is not None:
        return params

    # 5. Generic band-stats percentile fallback (also hrrr_smoke_*).
    params = _band1_percentile_rescale(raster_bytes)
    if params is not None:
        return params

    # 6. Safe, NEVER-empty default.
    logger.info(
        "publish_layer (titiler) no registry/stats match for preset=%r uri=%s - "
        "using safe default rescale",
        style_preset,
        layer_uri,
    )
    return _TITILER_SAFE_DEFAULT


def style_params_from_band_stats(
    style_preset: str | None,
    *,
    is_categorical: bool = False,
    is_rgba: bool = False,
    p2: float | None = None,
    p98: float | None = None,
    layer_uri: str = "",
) -> str:
    """Resolve TiTiler ``&rescale=..&colormap_name=..`` WITHOUT a COG download.

    The register-only fast path (SFINCS postprocess offload, Phase 4): the worker
    precomputes ``band_stats`` per COG, so the agent resolves the SAME style
    params ``_resolve_titiler_style_params`` would, but from the manifest stats
    instead of re-reading the COG. Resolution order mirrors that function exactly:

    1. CATEGORICAL passthrough (``is_categorical``) -> ``""`` (embedded palette
       wins).
    2. RGBA / multiband passthrough (``is_rgba``) -> ``""`` (baked colors render
       directly).
    3. TERRAIN-token passthrough (preset / uri tokenizes to a terrain token) ->
       ``""``.
    4. REGISTRY (exact key, then family substring/prefix) - flood/plume/wave +
       weather scalars pinned byte-for-byte.
    5. GENERIC fallback from ``p2``/``p98`` (NO COG read) - the manifest's
       precomputed substitute for ``_band1_percentile_rescale``.
    6. SAFE non-empty default.
    """
    # 1. Categorical / paletted -> embedded palette wins.
    if is_categorical:
        return ""
    # 2. RGBA / multiband composite -> TiTiler renders baked colors directly.
    if is_rgba:
        return ""
    # 3. Terrain-family preset/URI -> grayscale/RGBA terrain renders directly.
    if _is_terrain_token_preset(style_preset, layer_uri):
        return ""
    # 4. Typed registry (exact + family).
    params = _registry_style_params(style_preset or "")
    if params is not None:
        return params
    # 5. Generic percentile rescale from the worker-precomputed band stats.
    if p2 is not None and p98 is not None:
        lo, hi = float(p2), float(p98)
        if lo == lo and hi == hi:  # NaN guard
            if hi <= lo:
                pad = max(abs(lo) * 0.01, 1e-6)
                lo, hi = lo - pad, hi + pad
            return f"&rescale={lo:g},{hi:g}&colormap_name=viridis"
    # 6. Safe, NEVER-empty default.
    return _TITILER_SAFE_DEFAULT


# --------------------------------------------------------------------------- #
# Data-driven legend KEY (NATE: "the color gradient/key must come FROM THE DATA
# when we fetch the map -- it MUST mean something").
#
# The legend is derived DIRECTLY from the resolved TiTiler style_params string
# (the SAME ``&rescale=lo,hi&colormap_name=name`` the raster render uses), so the
# legend range and the painted raster range AGREE by construction -- there is no
# second, separately-computed range to drift. For pinned-registry presets that is
# the semantic fixed range (flood 0-3, seismic PGA 0-1, temperature 250-320 K);
# for the generic fallback it is the REAL p2/p98 percentile range the resolver
# already read off the COG. Categorical (paletted/NLCD) rasters carry NO
# style_params (the embedded GDAL table colorizes them), so their legend comes
# from ``_read_band1_colormap`` instead -- one ``LegendClass`` per table entry.
#
# Additive + fail-open: ANY failure here returns ``None`` so the publish proceeds
# exactly as before (legend=None => the web legacy style_preset path renders it).
# --------------------------------------------------------------------------- #

#: Module-level side-table of the most-recent published-raster ``LegendKey``
#: keyed by the layer's ENVELOPE uri - the raw ``s3://`` COG the atomic
#: ``publish_layer`` returns (TiTiler exit; formerly the tile TEMPLATE; the
#: register-only manifest seam now keys by the same raw ``cog_uri``, so both
#: producers share one key shape). ``publish_layer`` returns a bare URI string, so the
#: server wrap-site rebuilds a ``LayerURI`` from it WITHOUT a legend; the pipeline
#: emitter's ``add_loaded_layer`` lifts the legend back out of this stash by
#: ``layer.uri``. Mirrors ``_LAST_DENSITY_META_BY_URI`` exactly (module scope is
#: safe -- the legend is a pure function of the content-addressed COG + preset, so
#: two sessions publishing the same layer compute the identical key). FIFO-bounded
#: at the write site so the always-on agent process never grows it without limit.
_MAX_LEGEND_ENTRIES: int = 256
_LAST_LEGEND_BY_URI: dict[str, Any] = {}


def _parse_style_params(style_params: str) -> tuple[float | None, float | None, str | None]:
    """Pull ``(vmin, vmax, colormap_name)`` out of a ``&rescale=lo,hi&colormap_name=name``
    style-params string. Any field absent / unparseable -> ``None`` for that slot.

    This is the inverse of the strings the resolver builds, so the legend and the
    raster render are GUARANTEED to use the same numbers (no second range read).
    """
    from urllib.parse import parse_qsl

    vmin: float | None = None
    vmax: float | None = None
    cmap: str | None = None
    if not style_params:
        return (None, None, None)
    for k, v in parse_qsl(style_params.lstrip("&"), keep_blank_values=False):
        if k == "rescale" and "," in v:
            lo_s, hi_s = v.split(",", 1)
            try:
                vmin, vmax = float(lo_s), float(hi_s)
            except ValueError:
                vmin = vmax = None
        elif k == "colormap_name":
            cmap = v or None
    return (vmin, vmax, cmap)


def _rgb_to_hex(entry: Any) -> str | None:
    """``(r, g, b[, a])`` 0-255 ints -> ``"#rrggbb"``; ``None`` on a bad entry."""
    try:
        r, g, b = int(entry[0]), int(entry[1]), int(entry[2])
    except (TypeError, ValueError, IndexError):
        return None
    if not all(0 <= c <= 255 for c in (r, g, b)):
        return None
    return f"#{r:02x}{g:02x}{b:02x}"


def _categorical_legend_from_colormap(
    cmap: dict, *, label: str | None = None
) -> "LegendKey | None":
    """Build a categorical ``LegendKey`` from a band-1 GDAL color table.

    ``cmap`` is ``{class_index: (r, g, b, a)}`` (the shape ``_read_band1_colormap``
    returns for NLCD + other paletted rasters). One ``LegendClass`` per MEANINGFUL
    entry, ordered by class index. GDAL always materializes the table to 256
    entries; indices the raster does not actually use come back as either fully
    transparent (``a == 0`` -- nodata / unused slots) OR the opaque-black filler
    default ``(0, 0, 0, 255)``. Both are dropped so the legend shows only the
    classes that meaningfully colorize pixels (a real NLCD table has ~16 distinct
    colors, not 256). Duplicate colors are collapsed to the first class index that
    carries them (paletted rasters never reuse a color for two real classes). The
    label is the class index rendered verbatim (this seam carries no code->name
    map). Returns ``None`` when nothing meaningful survives.
    """
    from grace2_contracts.execution import LegendClass, LegendKey

    classes: list[LegendClass] = []
    seen_colors: set[str] = set()
    for idx in sorted(cmap.keys()):
        entry = cmap[idx]
        # Drop fully-transparent slots (nodata / unused class codes).
        try:
            if len(entry) >= 4 and int(entry[3]) == 0:
                continue
        except (TypeError, ValueError):
            pass
        hex_color = _rgb_to_hex(entry)
        if hex_color is None:
            continue
        # Drop GDAL's opaque-black filler default for unset palette indices.
        if hex_color == "#000000":
            continue
        # Collapse duplicate colors (a paletted raster gives each real class a
        # distinct color; repeats are filler echoes).
        if hex_color in seen_colors:
            continue
        seen_colors.add(hex_color)
        classes.append(
            LegendClass(value=int(idx), color=hex_color, label=str(int(idx)))
        )
    if not classes:
        return None
    return LegendKey(kind="categorical", classes=classes, label=label)


def legend_for_published_layer(
    style_preset: str | None,
    layer_uri: str,
    style_params: str,
    *,
    units: str | None = None,
    raster_bytes: bytes | None = None,
) -> "LegendKey | None":
    """Build the data-driven ``LegendKey`` for a just-published RASTER layer.

    Derived from the ALREADY-resolved ``style_params`` so the legend range equals
    the rendered range by construction:

    - ``style_params`` carries ``&rescale=lo,hi&colormap_name=name`` -> a
      ``kind="continuous"`` key with ``colormap=name``, ``vmin=lo``, ``vmax=hi``
      (the real p2/p98 range for unpinned presets; the pinned semantic range for
      registry presets -- whichever the raster actually renders with).
    - empty ``style_params`` (categorical / RGBA / terrain passthrough) -> probe
      the COG for an embedded GDAL color table and emit a ``kind="categorical"``
      key of one swatch per class. RGBA composites + grayscale terrain carry no
      table, so they get ``None`` (legacy rendering -- there is no meaningful key).

    Fail-open: returns ``None`` on ANY error so the publish is never blocked
    (``legend=None`` => the web legacy ``style_preset`` path renders the layer
    exactly as before).
    """
    from grace2_contracts.execution import LegendKey

    try:
        vmin, vmax, cmap_name = _parse_style_params(style_params)
        label = _legend_label_for(style_preset)
        if cmap_name is not None and vmin is not None and vmax is not None:
            # Continuous raster: the resolved rescale IS the legend range, so the
            # colorbar and the painted tiles span the identical numbers.
            return LegendKey(
                kind="continuous",
                colormap=cmap_name,
                vmin=vmin,
                vmax=vmax,
                units=units,
                label=label,
            )
        # No rescale/colormap in the URL -> categorical/paletted, RGBA, or
        # terrain passthrough. Only a paletted raster has a meaningful key.
        if raster_bytes is None:
            raster_bytes = _read_raster_bytes(layer_uri)
        if raster_bytes is None:
            return None
        try:
            import rasterio
            from rasterio.io import MemoryFile

            with MemoryFile(raster_bytes) as mem, mem.open() as src:
                table = _read_band1_colormap(src)
        except Exception as exc:  # noqa: BLE001 - palette probe is best-effort
            logger.debug(
                "legend palette probe skipped (%s: %s)", type(exc).__name__, exc
            )
            return None
        if not table:
            return None
        return _categorical_legend_from_colormap(table, label=label)
    except Exception as exc:  # noqa: BLE001 - never block a publish on the legend
        logger.debug(
            "legend_for_published_layer failed for %s (%s: %s)",
            layer_uri,
            type(exc).__name__,
            exc,
        )
        return None


def _legend_label_for(style_preset: str | None) -> str | None:
    """A short human-readable legend title from the preset, or ``None``.

    Best-effort cosmetic: ``"continuous_flood_depth"`` -> ``"Flood depth"``. The
    frontend renders it verbatim as the legend caption; ``None`` is fine (the web
    falls back to the layer name). Pure presentation -- never affects the range.
    """
    if not style_preset or style_preset == "auto":
        return None
    cleaned = style_preset
    for prefix in ("continuous_", "categorical_", "diverging_"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix) :]
            break
    cleaned = cleaned.replace("_", " ").strip()
    if not cleaned:
        return None
    return cleaned[:1].upper() + cleaned[1:]


def _stash_legend_for_uri(display_uri: str, legend: "LegendKey | None") -> None:
    """Record (or clear) the published layer's ``LegendKey`` keyed by envelope uri
    (the raw ``s3://`` COG - both the atomic publish and the register-only
    manifest seam key by it).

    FIFO-bounded (mirrors ``_LAST_DENSITY_META_BY_URI``) so the always-on agent
    process cannot grow this side-table without limit. A ``None`` legend clears
    any stale entry for this uri (so a re-publish that now resolves to no key
    cannot leave an orphaned one behind).
    """
    if not display_uri:
        return
    if display_uri in _LAST_LEGEND_BY_URI:
        del _LAST_LEGEND_BY_URI[display_uri]
    if legend is None:
        return
    _LAST_LEGEND_BY_URI[display_uri] = legend
    while len(_LAST_LEGEND_BY_URI) > _MAX_LEGEND_ENTRIES:
        _LAST_LEGEND_BY_URI.pop(next(iter(_LAST_LEGEND_BY_URI)))


def pop_legend_for_uri(display_uri: str) -> "LegendKey | None":
    """Look up the stashed ``LegendKey`` for a published layer's envelope uri
    (the raw ``s3://`` COG - atomic publish and register-only path alike).

    Non-destructive READ (a re-emit / replay of the SAME layer must resolve the
    same key). The pipeline emitter's ``add_loaded_layer`` calls this to lift the
    legend onto the ``ProjectLayerSummary`` for the publish_layer wrap-site path
    (where the rebuilt ``LayerURI`` carries no legend of its own). Returns
    ``None`` when nothing was stashed (legacy / categorical-RGBA layers).
    """
    return _LAST_LEGEND_BY_URI.get(display_uri)


# NOTE (TiTiler exit, 2026-07): ``build_titiler_tile_url`` - the legacy TiTiler
# XYZ tile-TEMPLATE mint (``{base}/cog/tiles/WebMercatorQuad/{z}/{x}/{y}.png
# ?url=<cog>&rescale=..``) - was DELETED once its last importer
# (``workflows.register_published_manifest``) swapped to emitting the raw
# ``s3://`` ``cog_uri`` and stashing its legend by that uri, exactly like the
# atomic publish above. Do not reintroduce a tile-template mint here.


# --------------------------------------------------------------------------- #
# F32: benign vector handling
# --------------------------------------------------------------------------- #

#: Vector artifact extensions. ``publish_layer`` is RASTER-ONLY (see the module
#: docstring + the Wave 4.9 inline-GeoJSON path). A vector reaching this tool is
#: ALREADY on the map via its producing fetch tool (``add_loaded_layer`` inline
#: GeoJSON), so a publish is unnecessary - and routing it through the raster tile
#: path mints HANGING tiles that freeze the map. Token-tail matched against the
#: resolved URI basename.
_VECTOR_EXTS = (
    ".fgb",
    ".geojson",
    ".json",
    ".geoparquet",
    ".parquet",
    ".gpkg",
    ".shp",
)


def _is_vector_uri(layer_uri: str) -> bool:
    """True when ``layer_uri`` names a vector artifact (by extension)."""
    return layer_uri.lower().rstrip("/").endswith(_VECTOR_EXTS)


def _benign_vector_noop(layer_uri: str, layer_id: str) -> str:
    """Return a calm, NON-ERROR signal for a vector handed to publish_layer (F32).

    The agent keeps calling ``publish_layer`` on vector layers (roads/rivers)
    that ALREADY rendered inline via their producing fetch tool's GeoJSON
    (Wave 4.9 ``add_loaded_layer`` path). Pre-F32 this RAISED
    ``PUBLISH_LAYER_VECTOR_NOT_RASTER`` → a scary red "Publishing layer… failed"
    card on a layer the user can already see.

    F32 turns that into a benign no-op: NO raise (so ``emit_tool_call``
    ``mark_complete``s the step - green, not red), NO tile template, NO
    ``observe_published_layer`` registration (so no hanging-tile face is minted).
    The returned string is the function_response the LLM reads - a clear,
    honest "already rendered inline; no publish needed" so it narrates calmly
    and does not retry.
    """
    logger.info(
        "publish_layer: benign vector no-op for layer_id=%s uri=%s - vector "
        "already rendered inline (Wave 4.9 GeoJSON); no raster publish needed",
        layer_id,
        layer_uri,
    )
    return (
        f"noop: layer_id={layer_id!r} is a VECTOR ({layer_uri!r}) and is already "
        "rendered on the map inline by its producing fetch tool (GeoJSON). "
        "publish_layer is raster-only; no publish was needed and none was "
        "performed. Do NOT re-call publish_layer for this vector layer."
    )


# --------------------------------------------------------------------------- #
# DATA-ISLAND #165 PHASE 0: durable browser-readable GeoJSON for every vector.
#
# Vectors are produced as FlatGeobuf (``.fgb``) which the browser CANNOT read,
# and today the agent delivers them INLINE (it reads the .fgb back, parses to
# GeoJSON, and ships the FeatureCollection on the WS). That works ONLY while the
# agent box is awake - the box-off cold path (signer -> S3) has no browser-
# readable copy of a vector layer, so a cold-opened case paints rasters but not
# roads/rivers/footprints/mesh.
#
# This phase FREEZES a durable contract: every vector publish materializes a
# GeoJSON FeatureCollection at a STABLE, per-Case key in the DURABLE runs bucket
# (the same bucket that holds the case-view snapshot + solver decks), so a later
# phase's case manifest / cold-view materializer can serve it with ZERO agent
# involvement. The .fgb stays the DATA face (analytical tools open it); the
# GeoJSON asset is the DISPLAY face (the browser fetches it).
#
# Frozen contract (engine tracks rebase onto this):
#   bucket : GRACE2_RUNS_BUCKET (solver._get_runs_bucket - the DURABLE runs
#            bucket, NOT the 30-day-TTL content-addressed cache bucket; a
#            published layer must outlive cache eviction).
#   key    : ``case-data/<case_id>/<layer_id>.geojson``
#   asset  : the returned ``s3://<runs_bucket>/case-data/<case_id>/<layer_id>.geojson``
#            URI - the DISPLAY face (resolved to a served/pre-signed URL by the
#            cold-view path, exactly like the case-view snapshot).
#   faces  : observe_published_layer(layer_id, gcs_uri=<s3 .fgb DATA>,
#            wms_url=<s3 .geojson DISPLAY>) - the GeoJSON never displaces the
#            data uri (mirrors the raster tile-template / WMS branches).
# --------------------------------------------------------------------------- #

#: Object-key prefix for durable per-Case vector GeoJSON assets in the runs
#: bucket. Single seam so this writer and the (future) cold-view materializer
#: name the object identically. Mirrors ``persistence.CASE_VIEWS_PREFIX``.
DURABLE_CASE_DATA_PREFIX: str = "case-data"


def durable_vector_geojson_key(case_id: str, layer_id: str) -> str:
    """Return the runs-bucket object key for a Case's durable vector GeoJSON.

    Frozen #165 Phase-0 contract: ``case-data/<case_id>/<layer_id>.geojson``.
    One seam so the writer (here) and any later reader name it identically.
    """
    return f"{DURABLE_CASE_DATA_PREFIX}/{case_id}/{layer_id}.geojson"


def _vector_uri_to_geojson_bytes(layer_uri: str) -> bytes | None:
    """Read a vector artifact URI and return UTF-8 GeoJSON FeatureCollection bytes.

    REUSES the existing read + parse helpers - does NOT reimplement them:
      - ``.fgb`` bytes -> ``pipeline_emitter._fgb_bytes_to_geojson`` (pyogrio +
        geopandas; the same converter the inline path uses).
      - ``.geojson`` / ``.json`` -> validated FeatureCollection passed through.

    Source bytes are read with the SAME boto3 EC2-instance-role client every
    other s3 download in this module uses (``cache.read_object_bytes_s3``); a
    local path is read directly (dev / test convenience). Returns ``None`` on
    ANY read / parse / unsupported-extension error (caller fails open).
    """
    import json as _json

    try:
        if layer_uri.startswith("s3://"):
            from .cache import read_object_bytes_s3

            raw = read_object_bytes_s3(layer_uri)
        elif layer_uri.startswith(("gs://", "/vsigs/")):
            # GCP is decommissioned; a gs:// vector here is unexpected on the
            # AWS data island. Fail open (caller -> benign no-op).
            return None
        else:
            with open(layer_uri, "rb") as f:
                raw = f.read()
    except Exception as exc:  # noqa: BLE001 - fail-open
        logger.warning(
            "publish_layer: durable-geojson source read failed uri=%s (%s: %s)",
            layer_uri,
            type(exc).__name__,
            exc,
        )
        return None

    ext = layer_uri.lower().rstrip("/").rsplit(".", 1)[-1] if "." in layer_uri else ""
    try:
        if ext == "fgb":
            from ..pipeline_emitter import _fgb_bytes_to_geojson

            obj = _fgb_bytes_to_geojson(raw)
            if obj is None:
                return None
        elif ext in {"geojson", "json"}:
            obj = _json.loads(raw)
            if not isinstance(obj, dict) or obj.get("type") != "FeatureCollection":
                logger.warning(
                    "publish_layer: durable-geojson source is not a "
                    "FeatureCollection uri=%s",
                    layer_uri,
                )
                return None
        else:
            logger.warning(
                "publish_layer: durable-geojson unsupported extension %r uri=%s",
                ext,
                layer_uri,
            )
            return None
        return _json.dumps(obj).encode("utf-8")
    except Exception as exc:  # noqa: BLE001 - fail-open
        logger.warning(
            "publish_layer: durable-geojson parse/dump failed uri=%s (%s: %s)",
            layer_uri,
            type(exc).__name__,
            exc,
        )
        return None


def _write_durable_vector_geojson(
    layer_uri: str, layer_id: str, case_id: str
) -> str | None:
    """Materialize a vector layer's GeoJSON to the DURABLE runs bucket (#165 P0).

    Reads ``layer_uri`` (FlatGeobuf / GeoJSON) to a GeoJSON FeatureCollection,
    writes it to ``s3://<runs_bucket>/case-data/<case_id>/<layer_id>.geojson``
    via the SAME boto3 EC2-instance-role client + runs-bucket convention every
    run artifact uses (``solver._get_runs_bucket``), and returns the durable
    ``s3://`` asset URI.

    FAIL-OPEN: returns ``None`` on ANY read / parse / write error (the caller
    degrades to the existing benign no-op - data-source-fallback norm). NEVER
    raises.
    """
    geojson_bytes = _vector_uri_to_geojson_bytes(layer_uri)
    if geojson_bytes is None:
        return None
    try:
        import boto3

        from .solver import _get_runs_bucket

        bucket = _get_runs_bucket()
        key = durable_vector_geojson_key(case_id, layer_id)
        s3 = boto3.client(
            "s3", region_name=os.environ.get("AWS_REGION", "us-west-2")
        )
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=geojson_bytes,
            ContentType="application/geo+json",
        )
        asset_uri = f"s3://{bucket}/{key}"
        logger.info(
            "publish_layer: durable vector GeoJSON written layer_id=%s case=%s "
            "asset=%s bytes=%d",
            layer_id,
            case_id,
            asset_uri,
            len(geojson_bytes),
        )
        return asset_uri
    except Exception as exc:  # noqa: BLE001 - fail-open
        logger.warning(
            "publish_layer: durable vector GeoJSON write failed layer_id=%s "
            "case=%s (%s: %s) - falling back to benign no-op",
            layer_id,
            case_id,
            type(exc).__name__,
            exc,
        )
        return None


# --------------------------------------------------------------------------- #
# F33: overview enforcement (no-overview COGs render spotty / never paint)
# --------------------------------------------------------------------------- #


def _raster_has_overviews(raster_bytes: bytes) -> bool | None:
    """True/False if the in-memory raster has internal overviews; None if unknown.

    Reads the bytes through a rasterio ``MemoryFile`` and inspects
    ``overviews(1)``. A non-empty list = overviews present. ``None`` is
    returned when rasterio is unavailable or the open fails - callers treat
    ``None`` as "cannot determine" and fail-open (publish as-is, legacy
    behavior).
    """
    try:
        import rasterio
        from rasterio.io import MemoryFile
    except Exception as exc:  # noqa: BLE001 - rasterio not installed
        logger.warning(
            "publish_layer: rasterio unavailable (%s) - cannot verify COG "
            "overviews; publishing as-is",
            exc,
        )
        return None
    try:
        with MemoryFile(raster_bytes) as mem, mem.open() as src:
            return bool(src.overviews(1))
    except Exception as exc:  # noqa: BLE001 - unreadable / not a raster
        logger.warning(
            "publish_layer: could not inspect raster overviews (%s: %s) - "
            "publishing as-is",
            type(exc).__name__,
            exc,
        )
        return None


def _read_band1_colormap(src) -> dict | None:
    """Return the band-1 palette color table (``{idx: (r,g,b,a)}``) or ``None``.

    NLCD land cover (and other categorical rasters) ship a single-band
    palette-index COG with an EMBEDDED GDAL color table; TiTiler colorizes from
    it. The F33 overview-enforcement re-write must carry that table forward or
    the layer renders solid grey (job-0324). rasterio raises ``ValueError`` when
    band 1 has no color table - the normal case for continuous rasters (DEM,
    hillshade, flood depth) - and we return ``None`` so callers do NOT fabricate
    one.
    """
    try:
        return src.colormap(1)
    except ValueError:
        return None
    except Exception as exc:  # noqa: BLE001 - any other read failure: no-op
        logger.debug("colormap read skipped (%s: %s)", type(exc).__name__, exc)
        return None


def _apply_band1_colormap(dst, cmap: dict | None) -> None:
    """Stamp a preserved band-1 color table + palette colorinterp onto ``dst``.

    No-op when ``cmap`` is ``None`` (non-paletted raster - never fabricate a
    color table). Otherwise writes the table on band 1 and marks band 1's color
    interpretation ``palette`` so TiTiler treats the integer pixels as indices.
    """
    if cmap is None:
        return
    try:
        dst.write_colormap(1, cmap)
        try:
            from rasterio.enums import ColorInterp

            interp = list(dst.colorinterp)
            interp[0] = ColorInterp.palette
            dst.colorinterp = tuple(interp)
        except Exception:  # noqa: BLE001 - colorinterp set is best-effort
            pass
    except Exception as exc:  # noqa: BLE001 - colormap copy is best-effort
        logger.warning(
            "publish_layer: colormap preservation failed (%s: %s); land-cover "
            "output may render grey",
            type(exc).__name__,
            exc,
        )


def _build_cog_with_overviews(raster_bytes: bytes) -> bytes | None:
    """Translate flat raster bytes into a tiled COG WITH overviews (F33).

    Strategy:
    1. PREFERRED - reuse ``compute_hillshade._translate_to_cog`` (the GDAL COG
       driver path the kickoff mandates: tiled + overviews in one pass). It
       resolves ``gdal_translate`` next to the ``gdaldem`` binary and falls
       back to flat bytes when the binary is missing - so we verify the result
       actually gained overviews before trusting it.
    2. FALLBACK - rasterio (``rio-cogeo`` if present, else a manual
       tiled-profile copy + ``build_overviews``) for environments without the
       GDAL CLI on PATH.

    Returns the new COG bytes, or ``None`` when no path could produce a real
    overview-bearing COG (caller then fails-open and publishes the original).
    """
    # 1. GDAL CLI path (reuse, do not reimplement - kickoff mandate).
    in_tmp: str | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as in_f:
            in_tmp = in_f.name
            in_f.write(raster_bytes)
        try:
            from .compute_hillshade import _get_gdaldem_bin, _translate_to_cog

            gdaldem_bin = _get_gdaldem_bin()  # raises if unavailable
            cog_bytes = _translate_to_cog(in_tmp, gdaldem_bin)
            # _translate_to_cog degrades to flat bytes when gdal_translate is
            # absent - verify overviews actually landed before trusting it.
            if _raster_has_overviews(cog_bytes):
                return cog_bytes
            logger.info(
                "publish_layer: GDAL _translate_to_cog produced no overviews "
                "(binary missing?) - trying rasterio fallback",
            )
        except Exception as exc:  # noqa: BLE001 - gdaldem unavailable / failed
            logger.info(
                "publish_layer: GDAL COG translate path unavailable (%s: %s) - "
                "trying rasterio fallback",
                type(exc).__name__,
                exc,
            )
    finally:
        if in_tmp is not None:
            try:
                os.unlink(in_tmp)
            except OSError:
                pass

    # 2. rasterio fallback (rio-cogeo preferred; manual overview build else).
    try:
        return _build_cog_with_overviews_rasterio(raster_bytes)
    except Exception as exc:  # noqa: BLE001 - fallback failed; fail-open upstream
        logger.warning(
            "publish_layer: rasterio COG/overview rebuild failed (%s: %s) - "
            "publishing original (no-overview) raster as-is",
            type(exc).__name__,
            exc,
        )
        return None


def _build_cog_with_overviews_rasterio(raster_bytes: bytes) -> bytes | None:
    """rasterio-only COG+overview rebuild (no GDAL CLI required)."""
    import rasterio
    from rasterio.io import MemoryFile

    # Detect a band-1 palette color table up front. When present (NLCD land
    # cover), SKIP the rio-cogeo path - its colormap forwarding is
    # version-dependent - and fall through to the manual build below, which
    # explicitly re-stamps the table (job-0324). Non-paletted rasters keep the
    # rio-cogeo fast path unchanged.
    with MemoryFile(raster_bytes) as probe_mem, probe_mem.open() as probe:
        has_colormap = _read_band1_colormap(probe) is not None

    # rio-cogeo is the cleanest path when installed (and the source is not a
    # palette raster whose color table we must guarantee).
    if not has_colormap:
        try:
            from rio_cogeo.cogeo import cog_translate
            from rio_cogeo.profiles import cog_profiles

            with MemoryFile(raster_bytes) as src_mem, src_mem.open() as src:
                dst_profile = cog_profiles.get("deflate")
                with MemoryFile() as dst_mem:
                    cog_translate(
                        src,
                        dst_mem.name,
                        dst_profile,
                        in_memory=True,
                        quiet=True,
                    )
                    out = dst_mem.read()
            if _raster_has_overviews(out):
                return out
        except Exception:  # noqa: BLE001 - rio-cogeo absent / failed; manual below
            logger.debug(
                "rio-cogeo path unavailable; manual overview build", exc_info=True
            )

    # Manual: copy into a tiled GTiff then build overviews in place.
    from rasterio.enums import Resampling

    with MemoryFile(raster_bytes) as src_mem, src_mem.open() as src:
        profile = src.profile.copy()
        profile.update(tiled=True, blockxsize=512, blockysize=512, compress="deflate")
        data = src.read()
        # Preserve a band-1 palette color table (e.g. NLCD land cover) across
        # the overview-enforcement re-write. None for non-paletted rasters
        # (DEM/hillshade/flood depth) - a pure no-op there (job-0324).
        cmap = _read_band1_colormap(src)
        # Palette rasters must downsample by NEAREST, never average - averaging
        # class indices produces meaningless in-between codes that map to wrong
        # colors. Continuous rasters keep average.
        overview_resampling = Resampling.nearest if cmap else Resampling.average
        with MemoryFile() as dst_mem:
            with dst_mem.open(**profile) as dst:
                dst.write(data)
                _apply_band1_colormap(dst, cmap)
                factors = _overview_factors(src.width, src.height)
                if factors:
                    dst.build_overviews(factors, overview_resampling)
                    dst.update_tags(
                        ns="rio_overview", resampling=overview_resampling.name
                    )
            out = dst_mem.read()
    return out if _raster_has_overviews(out) else None


def _overview_factors(width: int, height: int) -> list[int]:
    """Power-of-two decimation factors down to a ~256px overview (F33).

    For small rasters (max dimension < 512px) the 256px floor produces an empty
    list, so _build_cog_with_overviews_rasterio skips overview generation and the
    COG stays overview-free. TiTiler then computes minzoom == maxzoom for tiny
    COGs, and MapLibre silently renders nothing at the default CONUS zoom.

    Fix: always include at least factor=2, even if the 256px floor is never met.
    A single factor-2 overview (64-75px) is sufficient for TiTiler to lower its
    minzoom and for MapLibre to overzoom the tiles at any zoom level.
    """
    factors: list[int] = []
    factor = 2
    while max(width, height) // factor >= 256:
        factors.append(factor)
        factor *= 2
        if len(factors) >= 8:  # safety cap
            break
    # Always add factor=2 even when the image is already smaller than 512px so
    # TiTiler gets at least one overview level for tiny rasters.
    if not factors:
        factors = [2]
    return factors


def _read_raster_bytes(layer_uri: str) -> bytes | None:
    """Read raster bytes for an ``s3://`` / local URI (None on failure).

    Used by the F33 overview check. Fail-open: any read error returns ``None``
    so the publish proceeds with the original URI (legacy behavior).
    """
    try:
        if layer_uri.startswith("s3://"):
            from .cache import read_object_bytes_s3

            return read_object_bytes_s3(layer_uri)
        # local path (dev/test convenience)
        with open(layer_uri, "rb") as f:
            return f.read()
    except Exception as exc:  # noqa: BLE001 - fail-open
        logger.warning(
            "publish_layer: could not read raster bytes for overview check "
            "(%s: %s) - publishing as-is",
            type(exc).__name__,
            exc,
        )
        return None


def _split_s3_uri(uri: str) -> tuple[str, str] | None:
    """Split an ``s3://`` URI into ``(bucket, key)``, or ``None`` if not parseable."""
    if not uri.startswith("s3://"):
        return None
    rest = uri[len("s3://"):]
    slash = rest.find("/")
    if slash <= 0 or slash == len(rest) - 1:
        return None
    return rest[:slash], rest[slash + 1:]


def _write_overview_cog(layer_uri: str, cog_bytes: bytes) -> str | None:
    """Write the auto-translated COG alongside the source; return its URI (None on fail).

    A fresh ULID-suffixed sibling object so the original (no-overview) COG is
    never mutated in place and warm negative-caches don't poison the new path.
    Fail-open: returns ``None`` on any write error (caller publishes original).
    """
    parsed_s3 = _split_s3_uri(layer_uri)
    try:
        if layer_uri.startswith("s3://") and parsed_s3 is not None:
            import boto3

            bucket, key = parsed_s3
            dir_prefix = key.rsplit("/", 1)[0] + "/" if "/" in key else ""
            new_key = f"{dir_prefix}overviews/{new_ulid()}.tif"
            s3 = boto3.client(
                "s3", region_name=os.environ.get("AWS_REGION", "us-west-2")
            )
            s3.put_object(
                Bucket=bucket, Key=new_key, Body=cog_bytes, ContentType="image/tiff"
            )
            return f"s3://{bucket}/{new_key}"
        # local path: write a sibling file.
        base, _ext = os.path.splitext(layer_uri)
        new_path = f"{base}.ovr-{new_ulid()}.tif"
        with open(new_path, "wb") as f:
            f.write(cog_bytes)
        return new_path
    except Exception as exc:  # noqa: BLE001 - fail-open
        logger.warning(
            "publish_layer: could not write auto-translated overview COG "
            "(%s: %s) - publishing original raster as-is",
            type(exc).__name__,
            exc,
        )
        return None


def _ensure_raster_has_overviews(layer_uri: str) -> str:
    """Guarantee the published raster is a COG WITH overviews (F33).

    A no-overview COG renders SPOTTY (per-strip range requests time out cold;
    QGIS Server / TiTiler can't downsample for low zooms), so before a raster's
    tile template / WMS face is ever registered, validate the source COG has
    overviews. When missing, auto-translate to a tiled+overview COG (reusing
    ``compute_hillshade._translate_to_cog``, with a rasterio fallback), write it
    to a fresh sibling object, log the auto-translate, and publish THAT instead.

    Fail-open at every step: an unreadable raster, a missing rasterio, a failed
    translate, or a failed write all degrade to returning ``layer_uri``
    unchanged (legacy behavior - never blocks a publish).
    """
    raster_bytes = _read_raster_bytes(layer_uri)
    if raster_bytes is None:
        return layer_uri

    has_ovr = _raster_has_overviews(raster_bytes)
    if has_ovr is not False:
        # True (overviews present) or None (cannot determine) → publish as-is.
        return layer_uri

    logger.warning(
        "publish_layer: raster %s has NO overviews - a no-overview COG renders "
        "spotty / times out cold; auto-translating to a tiled COG with "
        "overviews before publishing (F33)",
        layer_uri,
    )
    cog_bytes = _build_cog_with_overviews(raster_bytes)
    if cog_bytes is None:
        return layer_uri

    new_uri = _write_overview_cog(layer_uri, cog_bytes)
    if new_uri is None:
        return layer_uri

    logger.warning(
        "publish_layer: F33 auto-translate complete - publishing overview COG "
        "%s in place of no-overview source %s",
        new_uri,
        layer_uri,
    )
    return new_uri


#: URI schemes ``publish_layer`` can actually consume. Anything scheme-shaped
#: outside this set (e.g. a fabricated ``qgis://project1``) or a bare token
#: with no scheme/path shape at all (e.g. ``'LayerURI_from_previous_step'``)
#: is an UNRESOLVED HANDLE: a real handle would have been substituted with its
#: registered URI by ``uri_registry.resolve_params`` before dispatch.
_CONSUMABLE_URI_SCHEMES = ("s3://", "gs://", "http://", "https://", "file://")


def _looks_like_unresolved_handle(layer_uri: str) -> bool:
    """True when ``layer_uri`` cannot be a consumable URI or filesystem path.

    OPEN-17 class (2026-07-13, live local-8B incident): small models call
    ``publish_layer`` in the SAME iteration as the producing tool with literal
    placeholders ('LayerURI_from_previous_step') or invented pseudo-URIs
    ('qgis://project1'). Those fail deep in the publish path with an
    unhelpful GDAL/storage error. This predicate gates them at the door so
    the tool raises a typed, self-correcting error that NAMES the actually
    available handles instead.

    Conservative by construction — everything a valid caller passes today is
    accepted: registered handles are already server-resolved to real URIs
    before the tool body runs; composers pass ``s3://``/``gs://``/tile-template
    URLs; ``/vsi*`` GDAL paths and absolute filesystem paths pass through.
    """
    v = (layer_uri or "").strip()
    if not v:
        return True
    # Angle brackets and literal ellipses are never valid in a real URI -
    # they are template-placeholder shapes, BOTH observed live 2026-07-13:
    # 'gs://<result-fetched_usgs_earthquakes-uri>' and
    # 's3://.../earthquakes_layer.fgb' (the latter slipped past a scheme
    # allowlist and hit the F32 benign vector no-op, minting a success-shaped
    # "Layer published" for a fabricated URI). Tile-template braces
    # ({z}/{x}/{y}) remain VALID input for the legacy tile-template unwrap
    # branch (old persisted cases), so braces are NOT placeholder markers.
    if "<" in v or ">" in v or "..." in v:
        return True
    if v.startswith("/vsi") or v.startswith("/") or v.startswith("\\"):
        return False  # GDAL virtual path / absolute filesystem path
    if any(v.startswith(scheme) for scheme in _CONSUMABLE_URI_SCHEMES):
        return False
    return True  # bare token (placeholder/handle) or unknown scheme


def _unknown_handle_error(layer_uri: str) -> "PublishLayerError":
    """Typed, retryable unknown-handle error naming the available handles."""
    from ..uri_registry import ambient_layer_handle_inventory

    handles = ambient_layer_handle_inventory(limit=8)
    if handles:
        inventory = (
            "available handles in this case: "
            + ", ".join(repr(h) for h in handles)
            + "; pass one verbatim"
        )
    else:
        inventory = (
            "no layers have been produced in this case yet - run a fetch or "
            "composer tool first"
        )
    return PublishLayerError(
        "UNKNOWN_LAYER_HANDLE",
        f"unknown layer handle {layer_uri!r}; {inventory}, or skip "
        f"publish_layer entirely - fetch and composer tools auto-publish "
        f"their own results.",
        retryable=True,
    )


def derive_layer_id(layer_uri: str, registry: Any | None = None) -> str:
    """Derive a stable ``layer_id`` when the caller omitted one (2026-07-08).

    Local 8B models omit ``publish_layer``'s ``layer_id`` entirely (live
    TypeError evidence). Derivation order:

    1. the registered layer handle whose URI equals the (already
       server-resolved) ``layer_uri`` - i.e. the producing tool's own
       ``layer_id`` (``uri_registry.lookup_handle_for_uri``; uses the ambient
       dispatch registry when ``registry`` is not passed);
    2. the URI basename stem, sanitized to ``[A-Za-z0-9_-]`` (QGIS layer name
       + WMS ``LAYERS=`` safe);
    3. a fresh ``layer-<ulid>`` when the stem is empty.
    """
    import re as _re
    from urllib.parse import urlparse as _urlparse

    from ..uri_registry import lookup_handle_for_uri

    handle = lookup_handle_for_uri(layer_uri, registry)
    if handle:
        return handle
    path = _urlparse(layer_uri).path if "://" in layer_uri else layer_uri
    base = path.rsplit("/", 1)[-1]
    stem = base.rsplit(".", 1)[0] if "." in base else base
    slug = _re.sub(r"[^A-Za-z0-9_-]+", "-", stem).strip("-")
    if slug:
        return slug
    return f"layer-{new_ulid()}"


#: Known ``style_preset`` -> human label. Extend as new presets land; presets
#: not listed here fall through to the token-cleanup path in
#: ``_label_from_style_preset`` (strip a family prefix, title-case the rest).
_STYLE_PRESET_LABELS: dict[str, str] = {
    "standard_hillshade": "Hillshade",
    "continuous_flood_depth": "Flood Depth",
    "continuous_slope_pct": "Slope",
    "categorical_aspect": "Aspect",
    "standard_colored_relief": "Colored Relief",
    "continuous_dem": "Elevation",
    "categorical_landcover": "Land Cover",
    "continuous_impervious_surface": "Impervious Surface",
    "diverging_bed_evolution": "Sediment Deposition",
}


def _looks_like_ulid(value: str) -> bool:
    """True for a 26-char Crockford-base32 ULID shape (case-insensitive).

    Matches ``new_ulid()``'s output shape without importing the ``ulid``
    package here — a cheap regex is enough to recognize "this is not a
    human name, it's an identifier" for the OPEN-9 name-derivation guard.
    """
    import re as _re

    return bool(_re.match(r"^[0-9A-HJKMNP-TV-Z]{26}$", value, _re.IGNORECASE))


def _looks_like_hash_or_id(value: str) -> bool:
    """True for a bare ULID, or a long hex/opaque cache-key-shaped token.

    Used to skip non-human URI path segments (e.g. a cache-key filename
    stem like ``a1b2c3d4e5f6...tif``) when deriving a name from the URI.
    """
    import re as _re

    if _looks_like_ulid(value):
        return True
    return bool(_re.match(r"^[0-9a-f]{12,64}$", value, _re.IGNORECASE))


def _label_from_style_preset(style_preset: str | None) -> str | None:
    """Human label for a ``style_preset``, or ``None`` if uninformative."""
    if not style_preset:
        return None
    label = _STYLE_PRESET_LABELS.get(style_preset)
    if label:
        return label
    if style_preset in ("auto", ""):
        return None
    import re as _re

    # Strip a family prefix (e.g. "continuous_"/"standard_"/"categorical_")
    # and title-case what remains, so an unlisted-but-descriptive preset
    # (e.g. "continuous_ndvi") still yields a readable label ("Ndvi").
    cleaned = _re.sub(r"^(standard_|continuous_|categorical_)", "", style_preset)
    cleaned = cleaned.replace("_", " ").replace("-", " ").strip()
    return cleaned.title() if cleaned else None


def _label_from_uri(layer_uri: str) -> str | None:
    """Human label from a source ``layer_uri`` path segment, or ``None``.

    Prefers the PARENT directory segment (e.g. ``.../hillshade/<hash>.tif``
    -> ``"hillshade"``) since the file stem is typically a cache hash or a
    bare ULID and not human-meaningful; falls back to the file stem itself
    when it IS human-shaped (no parent segment, or the parent is also
    opaque).
    """
    from urllib.parse import urlparse as _urlparse

    path = _urlparse(layer_uri).path if "://" in layer_uri else layer_uri
    segments = [s for s in path.split("/") if s]
    if not segments:
        return None
    stem = segments[-1].rsplit(".", 1)[0] if "." in segments[-1] else segments[-1]
    candidates = ([segments[-2]] if len(segments) >= 2 else []) + [stem]
    for cand in candidates:
        if not cand or _looks_like_hash_or_id(cand):
            continue
        cleaned = cand.replace("_", " ").replace("-", " ").strip()
        if cleaned:
            return cleaned.title()
    return None


def _short_disambiguator(layer_id: str) -> str:
    """Short suffix (last 4 alnum chars of ``layer_id``, else today's MMDD)
    so two derived names for the same family/preset don't collide in the
    UI's layer list."""
    import re as _re

    tail = _re.sub(r"[^A-Za-z0-9]", "", layer_id or "")[-4:]
    if tail:
        return tail.upper()
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%m%d")


def derive_readable_layer_name(
    name: str | None,
    layer_id: str,
    style_preset: str | None,
    layer_uri: str,
) -> str:
    """Derive a human-readable layer name for the UI's layer list (OPEN-9).

    Local 8B models routinely omit ``publish_layer``'s ``name``, and when
    ``layer_id`` ALSO degrades to a bare ULID (``derive_layer_id``'s last
    resort), the published layer showed up in the UI as e.g.
    ``'01KX5TEZ20BK86EE6DG8PSVFJK'`` — meaningless to the user. Precedence:

    1. an explicit, non-empty ``name`` that is not ITSELF a bare-ULID shape
       — returned VERBATIM, no disambiguator appended (the caller already
       chose it deliberately; second-guessing it would be surprising).
    2. ``style_preset`` mapped to a human label (e.g. ``"standard_hillshade"``
       -> ``"Hillshade"``).
    3. a human segment of the source ``layer_uri`` path (the parent
       directory / product-family segment — the file stem is typically a
       cache hash or a ULID).
    4. a generic ``"Layer"`` fallback.

    Cases 2-4 append a short disambiguator (``_short_disambiguator``) so two
    derived names for the same family don't collide in the UI list.
    INVARIANT: a bare-ULID name must never reach the layer summary when any
    better signal (an explicit name, a style_preset, or a URI segment) is
    available.
    """
    if name and name.strip() and not _looks_like_ulid(name.strip()):
        return name.strip()

    label = _label_from_style_preset(style_preset) or _label_from_uri(layer_uri)
    if not label:
        label = "Layer"
    return f"{label} {_short_disambiguator(layer_id)}"


# --------------------------------------------------------------------------- #
# Tool registration
# --------------------------------------------------------------------------- #

_PUBLISH_LAYER_METADATA = AtomicToolMetadata(
    name="publish_layer",
    ttl_class="live-no-cache",
    source_class="publish_layer",
    cacheable=False,
)


@register_tool(
    _PUBLISH_LAYER_METADATA,
    # Annotations: readOnlyHint=False (writes overview COGs / durable vector
    # GeoJSON assets to the object store), openWorldHint=False (object store +
    # tile server only; no public API), destructiveHint=True (re-registers an
    # existing layer handle's display face), idempotentHint=False (each call
    # can mint a fresh overview COG / durable asset).
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=True,
    idempotent_hint=False,
)
def publish_layer(
    layer_uri: str,
    layer_id: str | None = None,
    style_preset: str | None = None,
    project_qgs_uri: str | None = None,
    case_id: str | None = None,
    name: str | None = None,
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> str:
    """Publish a COG raster layer to the map.

    Resolves the raster's styling (rescale + colormap -> the data-driven
    legend), enforces COG overviews, registers the layer, and returns the
    raster's ``s3://`` COG URI string - the client (QGIS plugin) loads the
    COG directly via GDAL and renders it from the envelope's legend/style
    fields. Vectors are a benign no-op (they already render inline via
    their producing fetch tool's GeoJSON). Not cacheable (side-effect tool;
    registers layer faces + may write overview COGs).

    When to use:
        - After ``postprocess_flood``, ``compute_hillshade``, ``compute_slope``,
          ``compute_colored_relief``, ``compute_aspect``, or any other tool that
          returns a ``LayerURI`` with an ``s3://`` COG, when the user needs the
          layer displayed on the map.
        - As the final step in any workflow that produces a raster output -
          the COG is not visible until this tool runs.

    When NOT to use:
        - Publishing vector layers (FlatGeobuf/GeoJSON already render inline
          via their producing fetch tool).
        - Caching or re-fetching data (this is a side-effect tool; the cache
          shim is not invoked).

    Params:
        layer_uri: the producing tool's ``layer_id`` HANDLE (PREFERRED -
            job-0263 layer-handle indirection: the server resolves it to the
            exact ``s3://`` COG it recorded), or the ``s3://`` URI copied
            VERBATIM from the producing tool's result. NEVER construct or
            re-type an s3:// path from memory.
        layer_id: layer name for the published layer. Must be stable and
            unique within the Case (e.g. ``"flood-depth-peak-<run_id>"``).
            OPTIONAL (2026-07-08, small-model resilience): when omitted, the
            id is DERIVED - from the producing tool's registered ``layer_id``
            when the resolved ``layer_uri`` maps to a registered layer, else
            from the URI basename stem (sanitized), else a fresh
            ``layer-<ulid>``.
        style_preset: style preset name, or omit for AUTO selection
            (recommended): flood/plume depth COGs get the
            ``"continuous_flood_depth"`` ramp; terrain products (colored
            relief, hillshade, raw DEM) get default rendering, which is
            correct for RGBA/grayscale rasters - the flood ramp painted them
            invisible.
        project_qgs_uri: legacy ``.qgs`` project URI; consumed only by the
            dormant ``GRACE2_QGIS_WMS_BASE`` vector-WMS seam. Omit.
        case_id: optional Case identifier (FR-MP-6 / job-0121). When passed,
            the server wrapper resolves the case-scoped ``.qgs`` URI via
            ``case_lifecycle.ensure_case_qgs`` BEFORE invoking this tool;
            this parameter is a transport-only carrier so the LLM-visible
            tool surface is honest about Case context. The atomic tool body
            itself does not perform Persistence I/O - the server-side
            wrapper does the lazy-init and substitutes the resolved URI
            into ``project_qgs_uri``. Defaults to ``None`` (single-tenant
            demo path; OQ-62-QGS-MUTATION-CONFLICT preserved verbatim).
        name: OPTIONAL human-readable display name for the UI's layer list
            (OPEN-9, 2026-07-10). When given, used verbatim. When omitted
            (or the model passes it as an unadorned copy of ``layer_id``),
            a readable name is DERIVED server-side (``style_preset`` ->
            label, else a ``layer_uri`` path segment, else a generic
            fallback) so a bare ULID never reaches the layer summary. This
            parameter is a transport-only carrier - the atomic tool body
            does not consume it (it returns a bare URL string, not a
            LayerURI); the server-side wrap-site
            (``derive_readable_layer_name``) applies the same precedence
            when it constructs the ``LayerURI`` the client renders.

    Returns:
        The published raster's raw ``s3://`` COG URI string (the
        overview-enforced COG when F33 auto-translated). Suitable for direct
        use as a ``LayerURI.uri`` value - the QGIS plugin opens it via GDAL
        ``/vsicurl/`` and styles it from the envelope legend.

    Raises:
        PublishLayerError: on any failure (unknown layer handle, non-s3
            raster URI). The ``error_code`` attribute carries a
            SCREAMING_SNAKE_CASE code for the pipeline strip.

    FR-DC-6: This tool is uncacheable-by-construction (a side-effect tool
    that registers per-Case layer state). The cache shim is NOT invoked.

    Invariant 4 (Rendering): this tool IS the publish bridge. The ``s3://``
    COG reaches the map only after this call registers it and stashes its
    render legend.

    Invariant 6 (Metadata-payload pattern): the published layer is surfaced
    to the client via the layer-load envelope (``observe_published_layer``);
    persistence is DynamoDB (MongoDB was torn down 2026-06-16).

    Cross-tool dependencies:
        Upstream (consumes):
        - ``postprocess_flood`` (via ``run_model_flood_scenario``) - flood-depth
          COG ``LayerURI`` is the most common ``layer_uri`` input.
        - ``compute_hillshade`` / ``compute_colored_relief`` / ``compute_slope`` /
          ``compute_aspect`` / ``compute_impervious_surface`` - any tool that
          returns a raster ``LayerURI`` with an ``s3://`` URI.
        - ``clip_raster_to_polygon`` / ``clip_raster_to_bbox`` - clipped rasters
          passed to this tool for display-extent-scoped publication.
        Downstream (feeds):
        - QGIS plugin layer panel - the returned ``s3://`` COG URI is used
          directly as a ``LayerURI.uri`` value; the plugin loads it via GDAL
          ``/vsicurl/`` and applies the envelope legend as its renderer.
        - ``run_model_flood_scenario`` / ``run_model_flood_habitat_scenario`` -
          call this as the final step of the workflow chain.
    """
    # OPEN-17 (2026-07-13): unknown/placeholder handle guard. A registered
    # handle was already substituted with its real URI by the server's
    # ``uri_registry.resolve_params`` seam before this body runs, so a bare
    # token ('LayerURI_from_previous_step') or a fabricated scheme
    # ('qgis://project1') reaching this point can NEVER publish. Fail at the
    # door with a typed, retryable error that names the case's actually
    # available handles so a small model self-corrects instead of spiraling.
    if _looks_like_unresolved_handle(layer_uri):
        raise _unknown_handle_error(layer_uri)

    # 2026-07-08 small-model resilience: layer_id is optional. Local 8B models
    # call publish_layer without it (live TypeError: missing 1 required
    # positional argument: 'layer_id'). The server dispatch seam injects the
    # same derived id into params so the wrap-site emission still fires; this
    # in-tool derivation covers direct/programmatic callers.
    if not layer_id:
        layer_id = derive_layer_id(layer_uri)
        logger.info(
            "publish_layer: layer_id omitted - derived %r from layer_uri=%s",
            layer_id,
            layer_uri,
        )

    # OPEN-9: ``name`` is a transport-only carrier (see docstring) - the
    # actual LayerURI.name the client renders is computed by the server-side
    # wrap-site's ``derive_readable_layer_name`` call (it has the resolved
    # published URI + style_preset this function's caller does not see yet).
    # Logged here purely for observability of what the model actually sent.
    if name:
        logger.info("publish_layer: name=%r layer_id=%r", name, layer_id)

    # TiTiler EXIT (2026-07): rasters publish as their raw s3:// COG URI -
    # the QGIS plugin (the only client) opens the COG directly via GDAL
    # /vsicurl/ and styles it from the envelope legend. No tile server, no
    # GRACE2_TILE_SERVER_BASE, no XYZ template mint. The COG itself is the
    # published artifact; no .qgs mutation, no worker round-trip.
    #
    # LEGACY republish (was the sprint-14-aws job-0294c IDEMPOTENT guard):
    # old persisted cases (and pre-swap composer registrations) carry TiTiler
    # tile-TEMPLATE display URLs. A re-publish of one is NOT an error - UNWRAP
    # the embedded ``url=`` s3 COG (the same trick
    # ``export_case_to_qgis._unwrap_tile_template`` uses) and flow it through
    # the normal raster path below, so the envelope comes out in the NEW raw
    # ``s3://`` shape with a fresh legend stash. A template with no
    # recoverable COG is returned verbatim (degraded legacy behavior; the
    # plugin unwraps templates it rehydrates on its own).
    if layer_uri.startswith(("http://", "https://")) and "/cog/tiles/" in layer_uri:
        from .export_case_to_qgis import _unwrap_tile_template

        unwrapped = _unwrap_tile_template(layer_uri)
        if unwrapped != layer_uri and unwrapped.startswith("s3://"):
            logger.info(
                "publish_layer: legacy tile-template input unwrapped to its "
                "s3 COG layer_id=%s cog=%s",
                layer_id,
                unwrapped,
            )
            layer_uri = unwrapped
        else:
            logger.info(
                "publish_layer: legacy tile-template input with no "
                "recoverable s3 COG - returning verbatim layer_id=%s",
                layer_id,
            )
            return layer_uri
    # F32 (2026-06-16): publish_layer is RASTER-ONLY (see module docstring)
    # but is repeatedly handed VECTOR artifacts (roads/rivers .fgb/.geojson)
    # that ALREADY rendered inline via their producing fetch tool's GeoJSON
    # (Wave 4.9 ``add_loaded_layer`` path). Pre-F32 this RAISED a typed
    # terminal error → a scary red "Publishing layer… failed" card on a
    # layer the user can already see, AND TiTiler cannot read a FlatGeobuf
    # as a raster so wrapping it in a /cog tile template mints HANGING tiles
    # that freeze the map. F32: return a BENIGN, non-error result instead -
    # no raise (the step completes GREEN), no tile template, no
    # ``observe_published_layer`` registration (no hanging-tile face), and a
    # calm function_response so the agent narrates honestly and never
    # re-calls publish_layer for the vector.
    if _is_vector_uri(layer_uri):
        # job-0308 forward seam: WHEN the AWS QGIS Server is stood up and
        # GRACE2_QGIS_WMS_BASE is exported, route the vector through a
        # styled WMS GetMap face (mirrors the GCP ``_build_wms_url`` shape
        # MAP=<.qgs key>&LAYERS=<id>&... but pointed at the AWS WMS base).
        # This NO-OPs on the live stack TODAY: the var is unset until the
        # infra exists, so the existing benign no-op is returned and
        # behavior is byte-for-byte unchanged (vectors render inline via
        # their producing fetch tool's GeoJSON).
        wms_base = _get_qgis_wms_base()
        if wms_base:
            effective_qgs_uri = _get_effective_qgs_uri(project_qgs_uri)
            qgs_key = _parse_qgs_key(effective_qgs_uri)
            wms_url = _build_vector_wms_url(
                wms_base, layer_uri, layer_id, qgs_key
            )
            logger.info(
                "publish_layer (qgis-vector) layer_id=%s uri=%s wms=%s",
                layer_id,
                layer_uri,
                wms_url,
            )
            # Register BOTH faces: the s3:// vector (consumable DATA uri)
            # + the WMS GetMap URL (display face). ``_looks_like_wms``
            # routes the WMS URL to the wms/display slot so it never
            # displaces the s3:// data uri (mirrors the raster branch).
            observe_published_layer(
                layer_id, gcs_uri=layer_uri, wms_url=wms_url
            )
            return wms_url
        # DATA-ISLAND #165 PHASE 0: when no QGIS WMS base is configured
        # (the live stack TODAY) write a DURABLE, browser-readable GeoJSON
        # for this vector so the box-off cold path can paint it. The .fgb is
        # the browser-unreadable DATA face; the GeoJSON asset is the DISPLAY
        # face. ``case_id`` is threaded by the server wrapper
        # (``_invoke_tool_via_emitter``: ``params.setdefault("case_id", ...)``
        # for EVERY publish_layer call, raster OR vector) so an in-Case
        # vector publish reaches here with the Case bound. FAIL-OPEN: any
        # geopandas/read/write error returns the existing benign no-op
        # (data-source-fallback norm; never raise).
        if case_id:
            asset_uri = _write_durable_vector_geojson(
                layer_uri, layer_id, case_id
            )
            if asset_uri is not None:
                # Register BOTH faces: the s3:// .fgb stays the DATA uri,
                # the durable s3:// GeoJSON asset is the DISPLAY face. It is
                # routed via ``wms_url`` so it NEVER displaces the data uri
                # (mirrors the WMS / tile-template branches above).
                observe_published_layer(
                    layer_id, gcs_uri=layer_uri, wms_url=asset_uri
                )
                logger.info(
                    "publish_layer (durable-vector) layer_id=%s data=%s "
                    "display=%s",
                    layer_id,
                    layer_uri,
                    asset_uri,
                )
                return asset_uri
        # No Case context, or the durable write failed: fall back to the
        # existing benign no-op (vectors still render inline via their
        # producing fetch tool's GeoJSON while the agent box is awake).
        return _benign_vector_noop(layer_uri, layer_id)
    if not layer_uri.startswith("s3://"):
        raise PublishLayerError(
            "LAYER_URI_NOT_FOUND",
            f"layer_uri {layer_uri!r} is not an s3:// COG on this AWS "
            "deployment. Pass the producing tool's layer handle or its "
            "s3:// URI verbatim.",
            retryable=True,
        )
    # F33: a no-overview COG renders SPOTTY (per-strip range requests time
    # out cold; TiTiler can't downsample for low zooms), so validate the COG
    # has overviews and auto-translate to a tiled+overview COG before
    # minting the tile template. Fail-open (publishes as-is) on any error.
    layer_uri = _ensure_raster_has_overviews(layer_uri)

    # F51: Style -> render params. The resolver math is UNCHANGED (the render
    # chokepoint + honesty floor); only where its output LANDS moved - the
    # ``&rescale=..&colormap_name=..`` string no longer rides a tile-URL query
    # (TiTiler exit), it feeds the stashed LEGEND the plugin renders from.
    # _resolve_titiler_style_params is the single resolution point:
    #   - flood depths keep the blue ramp over 0-3 m; plume concentrations
    #     (job-0292b) keep the red ramp over 0-10 mg/L (byte-for-byte);
    #   - precip / temperature / wind / drought / fuel-moisture / satellite
    #     resolve to physically-correct registry bands;
    #   - anything unknown gets a band-1 2nd/98th-percentile auto-rescale
    #     (viridis) read from the COG bytes already in hand, with a SAFE
    #     non-empty default if the stats read fails;
    #   - CATEGORICAL guard: a COG with an embedded GDAL color table (NLCD
    #     land cover) gets NO rescale so the palette colorizes it (job-0324)
    #     - never washed out; the legend carries the palette classes.
    # _infer_style_preset is applied here for the auto/None case so the
    # raster path keeps the same default selection as before.
    effective_preset = style_preset
    if effective_preset is None or effective_preset == "auto":
        effective_preset = _infer_style_preset(layer_uri, layer_id)
    style_params = _resolve_titiler_style_params(effective_preset, layer_uri)
    # DATA-DRIVEN LEGEND: derive the render KEY from the SAME resolved
    # style_params (so the legend range equals the painted range by
    # construction) and stash it keyed by the ENVELOPE uri - the raw s3://
    # COG this call returns. publish_layer returns a bare URI string, so the
    # server wrap-site rebuilds a LayerURI WITHOUT a legend; the pipeline
    # emitter's add_loaded_layer lifts the legend back out of the stash by
    # layer.uri (which now equals this s3 uri). The legend carries the
    # colormap NAME + vmin/vmax (continuous) or palette classes
    # (categorical) - everything the plugin renderer needs alongside the
    # envelope's style_preset field. Fail-open: a None legend just clears
    # the stash entry and the plugin falls back to its style_preset/default
    # rendering exactly as before.
    try:
        _legend = legend_for_published_layer(
            effective_preset, layer_uri, style_params
        )
        _stash_legend_for_uri(layer_uri, _legend)
    except Exception as exc:  # noqa: BLE001 - legend never blocks a publish
        logger.debug(
            "publish_layer legend build skipped (%s: %s)",
            type(exc).__name__,
            exc,
        )
    logger.info(
        "publish_layer (raw-cog) layer_id=%s uri=%s style_params=%s",
        layer_id,
        layer_uri,
        style_params,
    )
    # job-0304: register the published layer in the session URI registry so
    # the ``flood-depth-peak-<id>``-style handle resolves to a consumable
    # DATA uri for downstream tools (Pelicun, zonal stats). With the TiTiler
    # exit there is no separate display face: the raw s3:// COG IS both the
    # data uri and the envelope uri the plugin renders.
    observe_published_layer(layer_id, gcs_uri=layer_uri)
    return layer_uri
