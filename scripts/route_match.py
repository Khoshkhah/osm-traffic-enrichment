"""
route_match — route-based map-matching of provider traffic segments onto OSM edges.

Wraps the graph-DTW matcher from the sibling `network_matching` library. Provider
segments are the SOURCE (A); OSM `driving.edges` are the DESTINATION graph (B), which is
dense, directed and well-connected — what the matcher wants as B. Each provider segment is
matched to a *connected route* of OSM edges (so it can never jump to a topologically
disconnected parallel road); the per-edge results are then grouped back per OSM edge.

**No filtering.** Per OSM edge we take the **maximum-covering** candidate, and store the winner's
two numbers so matches can be filtered later via SQL instead of at match time:

  covering_match : % of the OSM edge the winning segment covers (integer 0–100)
  quality_match  : 100 · geometric alignment = round(100·exp(-drift/tau)·max(0,cos(bearing)))  (0–100)

Result is exactly one row per OSM edge. (`segment_match_status` below still uses the library's
`resolve_routes` — that's a separate diagnostic for the NO_MATCH explorer, not the match path.)

Returns:
    edge_level : dict[edge_id -> congestion level]   (one entry == one DuckDB row)
    edge_extra : dict[edge_id -> {covering_match, quality_match, *extra_cols}]
    report     : dict of counts for logging
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd

from network_matching import DuckDBMapMatcher

# resolve_routes (Filter 1) only understands these keys.
_RESOLVE_KEYS = ("max_match_dist", "max_bearing_diff", "min_overlap_pct")


def load_match_thresholds(path: str | Path = "config/match_thresholds.yaml") -> dict:
    """Load the per-provider threshold file (see scripts/calibrate_thresholds.py).
    Returns {} if the file is missing so callers fall back to built-in defaults."""
    import yaml
    p = Path(path)
    if not p.exists():
        return {}
    with open(p) as f:
        return yaml.safe_load(f) or {}


def utm_srid_for(gdf_or_geom) -> int:
    """A metre-based UTM SRID (EPSG:326xx N / 327xx S) from the geometry centroid."""
    geom = gdf_or_geom.geometry.union_all() if hasattr(gdf_or_geom, "geometry") else gdf_or_geom
    c = geom.centroid
    zone = int((c.x + 180) // 6) + 1
    return (32600 if c.y >= 0 else 32700) + zone


def run_match_routes(edges_gdf: gpd.GeoDataFrame, segments_gdf: gpd.GeoDataFrame,
                     *, utm_srid: int | None = None, level_col: str = "congestion",
                     extra_cols: tuple[str, ...] = (),
                     max_distance: float = 25.0, step_meters: float = 10.0,
                     snap_tolerance_m: float = 0.75, n_jobs: int = -1):
    """
    Prepare A/B inputs and run the raw graph-DTW match.

    Returns (routes_summary, routes_long, seg) where `seg` is the exploded segment
    GeoDataFrame carrying `seg_id` + `level_col` + `extra_cols` (for the id→attr join).
    Returns (None, None, seg) if there are no usable segments.
    """
    seg = segments_gdf[segments_gdf.geometry.geom_type.isin(["LineString", "MultiLineString"])]
    seg = seg[seg[level_col] != "no data"]
    seg = seg.explode(index_parts=False)
    seg = seg[seg.geometry.geom_type == "LineString"]
    if seg.empty:
        return None, None, seg
    seg = seg.to_crs("EPSG:4326").reset_index(drop=True)
    seg["seg_id"] = range(len(seg))
    if utm_srid is None:
        utm_srid = utm_srid_for(seg)

    edges = edges_gdf[edges_gdf.geometry.geom_type.isin(["LineString", "MultiLineString"])]
    edges = edges.to_crs("EPSG:4326")

    keep = [level_col, *extra_cols]
    with tempfile.TemporaryDirectory() as d:
        a_csv, b_csv = Path(d) / "segments.csv", Path(d) / "edges.csv"
        sa = seg[["seg_id", *keep]].copy()
        sa["geometry"] = seg.geometry.to_wkt()
        sa.to_csv(a_csv, index=False)
        sb = edges[["edge_id"]].copy()
        sb["geometry"] = edges.geometry.to_wkt()
        sb.to_csv(b_csv, index=False)

        m = DuckDBMapMatcher.from_wkt_csv(
            str(a_csv), str(b_csv), id_a="seg_id", id_b="edge_id",
            utm_srid=utm_srid, max_distance=max_distance, keep_cols_a=keep,
        )
        routes_long, routes_summary = m.match_routes(
            step_meters=step_meters, snap_tolerance_m=snap_tolerance_m, n_jobs=n_jobs,
        )
    return routes_summary, routes_long, seg


def route_match_segments(
    edges_gdf: gpd.GeoDataFrame,
    segments_gdf: gpd.GeoDataFrame,
    *,
    utm_srid: int | None = None,
    level_col: str = "congestion",
    extra_cols: tuple[str, ...] = (),
    tau: float = 15.0,                   # metres; drift scale in the quality (alignment) score
    max_distance: float = 25.0,
    step_meters: float = 10.0,
    snap_tolerance_m: float = 0.75,
    n_jobs: int = -1,
) -> tuple[dict[int, str], dict[int, dict], dict]:
    """
    Match `segments_gdf` (provider traffic, with `level_col`) onto `edges_gdf` (OSM, with
    `edge_id`). **No filtering**: every OSM edge takes its **maximum-covering** candidate.

    The winner's two numbers are returned (integers 0–100) so they can be stored and filtered
    later via SQL:
        covering_match : % of the OSM edge the winning segment covers (round of edge_b_used_pct)
        quality_match  : 100 · geometric alignment = round(100 · exp(-drift/tau) · max(0, cos(bearing)))

    extra_cols : provider columns carried to the winning edge (e.g. traffic_level).
    Returns (edge_level, edge_extra, report); edge_extra[edge_id] holds
    {covering_match, quality_match, *extra_cols}.
    """
    rs, rl, seg = run_match_routes(
        edges_gdf, segments_gdf, utm_srid=utm_srid, level_col=level_col,
        extra_cols=extra_cols, max_distance=max_distance, step_meters=step_meters,
        snap_tolerance_m=snap_tolerance_m, n_jobs=n_jobs,
    )

    report: dict = {"segments_parts": int(len(seg))}
    if rs is None or rl is None or len(rl) == 0:
        report.update(routes_matched=0,
                      segments_unmatched=report["segments_parts"], edges_assigned=0)
        return {}, {}, report

    report["routes_matched"] = int((rs["match_type"] != "NO_MATCH").sum())
    report["segments_unmatched"] = report["segments_parts"] - report["routes_matched"]

    # Per candidate: covering (% of the OSM edge used) and quality (geometric alignment).
    rl = rl.copy()
    rl["covering_match"] = rl["edge_b_used_pct"].astype(float)
    rl["quality_match"] = (np.exp(-rl["edge_match_dist_avg"].astype(float) / tau)
                           * np.clip(np.cos(np.radians(rl["edge_bearing_diff"].astype(float))), 0, None))

    keep = [level_col, *extra_cols]
    attrs = seg[["seg_id", *keep]].rename(columns={"seg_id": "source_id"})
    rl = rl.merge(attrs, on="source_id", how="left")

    # ── Assign — maximum-covering candidate per OSM edge (no filter) ──
    edge_level: dict[int, str] = {}
    edge_extra: dict[int, dict] = {}
    win_idx = rl.groupby("dest_id")["edge_matched_len"].idxmax()
    for _, row in rl.loc[win_idx].iterrows():
        eid = int(row["dest_id"])
        edge_level[eid] = row[level_col]
        ex = {"covering_match": int(round(float(row["covering_match"]))),          # 0–100
              "quality_match":  int(round(float(row["quality_match"]) * 100))}      # 0–100
        for c in extra_cols:
            ex[c] = None if pd.isna(row[c]) else row[c]
        edge_extra[eid] = ex

    report["edges_assigned"] = len(edge_level)
    return edge_level, edge_extra, report


def segment_match_status(
    edges_gdf: gpd.GeoDataFrame,
    segments_gdf: gpd.GeoDataFrame,
    *,
    utm_srid: int | None = None,
    level_col: str = "congestion",
    extra_cols: tuple[str, ...] = (),
    segment_match: dict | None = None,
    max_distance: float = 25.0,
    step_meters: float = 10.0,
    snap_tolerance_m: float = 0.75,
    n_jobs: int = -1,
) -> gpd.GeoDataFrame:
    """
    Return the exploded provider segments with a `match_status` column
    ('matched' | 'no_match') after Filter 1. For validating that the segments the
    pipeline discards as NO_MATCH genuinely have no good OSM counterpart.
    """
    rs, rl, seg = run_match_routes(
        edges_gdf, segments_gdf, utm_srid=utm_srid, level_col=level_col,
        extra_cols=extra_cols, max_distance=max_distance, step_meters=step_meters,
        snap_tolerance_m=snap_tolerance_m, n_jobs=n_jobs,
    )
    if rs is None:
        return seg.assign(match_status="no_match")

    f1 = {k: float(v) for k, v in (segment_match or {}).items()
          if k in _RESOLVE_KEYS and v is not None}
    if f1:
        from network_matching import DuckDBMapMatcher as _MM
        rs, rl = _MM().resolve_routes(rs, rl, **f1)

    matched_ids = set(rs.loc[rs["match_type"] != "NO_MATCH", "source_id"])
    seg = seg.copy()
    seg["match_status"] = seg["seg_id"].map(
        lambda s: "matched" if s in matched_ids else "no_match"
    )
    return seg


def format_report(report: dict, source: str) -> list[str]:
    """Render the match outcome as log lines (no filtering — max-cover selection)."""
    parts     = report.get("segments_parts", 0)
    unmatched = report.get("segments_unmatched", 0)
    pct = (100.0 * unmatched / parts) if parts else 0.0
    return [
        f"  Match ({source}, no filter — max cover):",
        f"    segment parts        : {parts:,}",
        f"    matched / unmatched  : {report.get('routes_matched', 0):,} / {unmatched:,} ({pct:.1f}%)",
        f"    OSM edges assigned   : {report.get('edges_assigned', 0):,}",
    ]
