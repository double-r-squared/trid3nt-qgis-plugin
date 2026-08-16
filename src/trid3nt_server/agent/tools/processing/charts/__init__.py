"""Chart-emission processing tools (Vega-Lite interactive charts).

Home of ``generate_chart`` -- the ONE generic interactive-chart primitive that
replaced the four fixed-shape tools (histogram / time-series / damage-distribution
/ choropleth-legend) in the processing-wave cull (docs/decisions/0043). The chart
SHAPE is now the caller's Vega-Lite spec; the shared emission core lives in
``processing.charts_common``.
"""
