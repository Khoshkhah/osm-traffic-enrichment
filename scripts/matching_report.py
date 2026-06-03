"""
matching_report — analyse how well Mapbox / TomTom traffic segments map-match
onto OSM driving edges.

This reproduces the EXACT geometric matching the pipeline uses
(`scripts.mapbox_traffic` / `scripts.tomtom_traffic`: 25 m corridor, ≤ 45° bearing,
≥ 40% overlap) but records full *provenance* — every (segment → edge) pair, with the
overlap fraction and bearing difference — so we can measure things the DuckDB history
tables cannot, because they collapse everything to "most severe wins per edge":

  * how many OSM edges got matched vs left unmatched
  * how many provider segments matched nothing (orphans)
  * cardinality:  M:1  (one edge claimed by several segments)
                  1:N  (one segment spread over several edges)
  * conflict rate: among M:1 edges, how often the segments disagreed on the level

The matching uses each source's own `_bearing` / `_dir_diff`, so the report reflects
production behaviour exactly (including the `_dir_diff` wrap quirk).

Usage (from repo root, env `osm-traffic-enrichment`):
    from scripts.matching_report import build_report, print_text_report
    rep = build_report("sodermalm", "mapbox")
    print_text_report(rep)
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import geopandas as gpd
import numpy as np
import pandas as pd

from scripts.mapbox_traffic import _bearing as _mb_bearing, _dir_diff as _mb_dirdiff
from scripts.tomtom_traffic import _bearing as _tt_bearing, _dir_diff as _tt_dirdiff

# Matching parameters — must mirror the pipeline classes.
BUF_M          = 25
DIR_THRESH     = 45
OVERLAP_THRESH = 0.40

SOURCE_CFG = {
    "mapbox": {"bearing": _mb_bearing, "dirdiff": _mb_dirdiff},
    "tomtom": {"bearing": _tt_bearing, "dirdiff": _tt_dirdiff},
}

# OSM highway classes, most important first (see feedback: highway sort order).
HIGHWAY_ORDER = [
    "motorway", "motorway_link", "trunk", "trunk_link",
    "primary", "primary_link", "secondary", "secondary_link",
    "tertiary", "tertiary_link", "unclassified", "residential",
    "living_street", "service", "pedestrian", "track", "road",
    "path", "footway", "cycleway", "steps",
]
_HW_RANK = {h: i for i, h in enumerate(HIGHWAY_ORDER)}


def _hw_rank(h) -> int:
    return _HW_RANK.get(h, len(HIGHWAY_ORDER))


# ── Loading ───────────────────────────────────────────────────────────────────

def load_edges(db_path: str | Path) -> gpd.GeoDataFrame:
    """Load driving.edges as a GeoDataFrame (EPSG:4326), lines only."""
    con = duckdb.connect(str(db_path), read_only=True)
    con.execute("INSTALL spatial")
    con.execute("LOAD spatial")
    df = con.execute(
        "SELECT edge_id, highway, name, length_m, ST_AsText(geometry) AS wkt_geom "
        "FROM driving.edges"
    ).df()
    con.close()
    edges = gpd.GeoDataFrame(
        df.drop(columns=["wkt_geom"]),
        geometry=gpd.GeoSeries.from_wkt(df["wkt_geom"]),
        crs="EPSG:4326",
    )
    return edges[edges.geometry.geom_type.isin(["LineString", "MultiLineString"])].reset_index(drop=True)


def load_segments(area: str, source: str,
                  output_dir: str | Path = "output") -> gpd.GeoDataFrame:
    """
    Load the cached provider segment GeoJSON that the pipeline fed into map_match,
    filtered the same way the matcher filters it: line geometries with real
    congestion (drop 'no data').
    """
    path = Path(output_dir) / f"{area}_traffic_{source}.geojson"
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found — run pipeline_traffic.py for '{area}' first "
            f"(it caches the provider segments there)."
        )
    g = gpd.read_file(path).to_crs("EPSG:4326")
    g = g[g.geometry.geom_type.isin(["LineString", "MultiLineString"])]
    g = g[g["congestion"] != "no data"].reset_index(drop=True)
    return g


# ── Matching (with provenance) ─────────────────────────────────────────────────

def match_with_provenance(edges: gpd.GeoDataFrame, segments: gpd.GeoDataFrame,
                          source: str) -> pd.DataFrame:
    """
    Run the pipeline's corridor/bearing/overlap match and return EVERY accepted
    (segment → edge) pair (before the most-severe collapse).

    Returns a DataFrame with one row per matched pair:
        seg_idx, edge_id, seg_congestion, overlap_frac, bear_diff
    """
    bearing = SOURCE_CFG[source]["bearing"]
    dirdiff = SOURCE_CFG[source]["dirdiff"]

    edges_m = edges.to_crs("EPSG:3857").reset_index(drop=True)
    seg_m   = segments.to_crs("EPSG:3857").reset_index(drop=True)
    sindex  = edges_m.sindex
    edge_ids = edges_m["edge_id"].to_numpy()

    rows: list[dict] = []
    for s_idx, seg in seg_m.iterrows():
        g_seg   = seg.geometry
        cong    = seg["congestion"]
        corridor = g_seg.buffer(BUF_M)
        s_bear   = bearing(g_seg)
        for i in sindex.intersection(corridor.bounds):
            g_edge = edges_m.geometry.iloc[i]
            bdiff  = dirdiff(s_bear, bearing(g_edge))
            if bdiff > DIR_THRESH:
                continue
            ov = corridor.intersection(g_edge).length / max(g_edge.length, 1e-6)
            if ov < OVERLAP_THRESH:
                continue
            rows.append({
                "seg_idx":        int(s_idx),
                "edge_id":        int(edge_ids[i]),
                "seg_congestion": cong,
                "overlap_frac":   round(float(ov), 4),
                "bear_diff":      round(float(bdiff), 2),
            })
    return pd.DataFrame(rows, columns=["seg_idx", "edge_id", "seg_congestion",
                                       "overlap_frac", "bear_diff"])


# ── Report assembly ────────────────────────────────────────────────────────────

_SEVERITY = {"low": 1, "moderate": 2, "heavy": 3, "severe": 4}


def build_report(area: str, source: str,
                 db_dir: str | Path = "db",
                 output_dir: str | Path = "output") -> dict:
    """
    Build the full matching report for one area + source.

    Returns a dict holding the raw frames (for plotting in a notebook) plus
    pre-computed summary tables and a flat `metrics` dict.
    """
    db_path  = Path(db_dir) / f"{area}.duckdb"
    edges    = load_edges(db_path)
    segments = load_segments(area, source, output_dir)
    matches  = match_with_provenance(edges, segments, source)

    n_edges = len(edges)
    n_segs  = len(segments)

    matched_edge_ids = set(matches["edge_id"].unique()) if len(matches) else set()
    n_matched   = len(matched_edge_ids)
    n_unmatched = n_edges - n_matched

    matched_seg_idx = set(matches["seg_idx"].unique()) if len(matches) else set()
    n_orphan_segs   = n_segs - len(matched_seg_idx)

    # Cardinality
    if len(matches):
        segs_per_edge = matches.groupby("edge_id")["seg_idx"].nunique()
        edges_per_seg = matches.groupby("seg_idx")["edge_id"].nunique()
    else:
        segs_per_edge = pd.Series(dtype=int)
        edges_per_seg = pd.Series(dtype=int)

    n_m_to_1 = int((segs_per_edge >= 2).sum())   # one edge, many segments
    n_1_to_n = int((edges_per_seg >= 2).sum())   # one segment, many edges

    # Conflict among M:1 edges: do the contributing segments disagree on level?
    n_conflict = 0
    if len(matches):
        per_edge_levels = matches.groupby("edge_id")["seg_congestion"].nunique()
        n_conflict = int(((segs_per_edge >= 2) & (per_edge_levels >= 2)).sum())

    # Length-based coverage (km) — more meaningful than edge counts
    km_total   = float(edges["length_m"].sum()) / 1000
    km_matched = float(edges.loc[edges["edge_id"].isin(matched_edge_ids), "length_m"].sum()) / 1000

    # ── Per-highway coverage table ────────────────────────────────────────────
    e = edges.copy()
    e["matched"] = e["edge_id"].isin(matched_edge_ids)
    hw = (e.groupby("highway")
            .agg(edges=("edge_id", "size"),
                 matched=("matched", "sum"),
                 km=("length_m", lambda s: round(s.sum() / 1000, 2)))
            .reset_index())
    hw["matched_pct"] = (hw["matched"] / hw["edges"] * 100).round(1)
    hw["_rank"] = hw["highway"].map(_hw_rank)
    hw = hw.sort_values("_rank").drop(columns="_rank").reset_index(drop=True)

    # ── Cardinality histograms ────────────────────────────────────────────────
    spe_hist = (segs_per_edge.value_counts().sort_index()
                .rename_axis("segments_per_edge").reset_index(name="edges"))
    eps_hist = (edges_per_seg.value_counts().sort_index()
                .rename_axis("edges_per_segment").reset_index(name="segments"))

    metrics = {
        "area": area, "source": source,
        "n_edges": n_edges, "n_segments": n_segs,
        "n_matched": n_matched, "n_unmatched": n_unmatched,
        "matched_pct": round(n_matched / n_edges * 100, 1) if n_edges else 0.0,
        "km_total": round(km_total, 1), "km_matched": round(km_matched, 1),
        "km_matched_pct": round(km_matched / km_total * 100, 1) if km_total else 0.0,
        "n_orphan_segments": n_orphan_segs,
        "orphan_pct": round(n_orphan_segs / n_segs * 100, 1) if n_segs else 0.0,
        "n_pairs": len(matches),
        "n_M_to_1_edges": n_m_to_1,
        "M_to_1_pct_of_matched": round(n_m_to_1 / n_matched * 100, 1) if n_matched else 0.0,
        "n_1_to_N_segments": n_1_to_n,
        "max_segs_per_edge": int(segs_per_edge.max()) if len(segs_per_edge) else 0,
        "max_edges_per_seg": int(edges_per_seg.max()) if len(edges_per_seg) else 0,
        "n_conflict_edges": n_conflict,
        "conflict_pct_of_M_to_1": round(n_conflict / n_m_to_1 * 100, 1) if n_m_to_1 else 0.0,
        "overlap_median": round(float(matches["overlap_frac"].median()), 3) if len(matches) else None,
        "bear_diff_median": round(float(matches["bear_diff"].median()), 2) if len(matches) else None,
    }

    return {
        "metrics": metrics,
        "edges": edges,
        "segments": segments,
        "matches": matches,
        "matched_edge_ids": matched_edge_ids,
        "segs_per_edge": segs_per_edge,
        "edges_per_seg": edges_per_seg,
        "highway_table": hw,
        "segs_per_edge_hist": spe_hist,
        "edges_per_seg_hist": eps_hist,
    }


def annotate_edges(rep: dict) -> gpd.GeoDataFrame:
    """
    Return the edges GeoDataFrame with two extra columns for mapping/inspection:
        matched     : bool — did any segment match this edge
        n_segments  : int  — how many segments matched it (0, 1, 2+ ⇒ M:1)
    """
    edges = rep["edges"].copy()
    spe   = rep["segs_per_edge"]
    edges["n_segments"] = edges["edge_id"].map(spe).fillna(0).astype(int)
    edges["matched"]    = edges["n_segments"] > 0
    return edges


def conflict_table(rep: dict, limit: int = 20) -> pd.DataFrame:
    """
    M:1 edges where the matching segments DISAGREED on the congestion level
    (these are the edges where "most severe wins" silently discarded a
    competing reading). One row per such edge with the set of levels seen.
    """
    m = rep["matches"]
    if not len(m):
        return pd.DataFrame(columns=["edge_id", "n_segments", "levels", "kept_most_severe"])
    g = (m.groupby("edge_id")
           .agg(n_segments=("seg_idx", "nunique"),
                levels=("seg_congestion", lambda s: sorted(set(s), key=lambda x: _SEVERITY.get(x, 0))))
           .reset_index())
    g = g[(g["n_segments"] >= 2) & (g["levels"].map(len) >= 2)]
    g["kept_most_severe"] = g["levels"].map(lambda lv: max(lv, key=lambda x: _SEVERITY.get(x, 0)))
    names = rep["edges"].set_index("edge_id")["name"]
    g["name"] = g["edge_id"].map(names)
    return g.sort_values("n_segments", ascending=False).head(limit).reset_index(drop=True)


def compare_sources(rep_a: dict, rep_b: dict) -> dict:
    """Cross-source comparison of matched-edge coverage and level agreement."""
    a, b = rep_a["metrics"]["source"], rep_b["metrics"]["source"]
    set_a, set_b = rep_a["matched_edge_ids"], rep_b["matched_edge_ids"]
    both = set_a & set_b

    # winning (most severe) level per edge for each source
    def winner(rep):
        m = rep["matches"]
        if not len(m):
            return {}
        m = m.assign(sev=m["seg_congestion"].map(_SEVERITY))
        idx = m.groupby("edge_id")["sev"].idxmax()
        return dict(zip(m.loc[idx, "edge_id"], m.loc[idx, "seg_congestion"]))

    win_a, win_b = winner(rep_a), winner(rep_b)
    agree = sum(1 for e in both if win_a.get(e) == win_b.get(e))

    return {
        "source_a": a, "source_b": b,
        "matched_a": len(set_a), "matched_b": len(set_b),
        "both": len(both),
        "only_a": len(set_a - set_b), "only_b": len(set_b - set_a),
        "agree_on_level": agree,
        "agree_pct": round(agree / len(both) * 100, 1) if both else 0.0,
    }


# ── Text rendering (for CLI / quick sanity) ────────────────────────────────────

def print_text_report(rep: dict) -> None:
    m = rep["metrics"]
    print(f"\n{'='*60}")
    print(f"  Matching report — {m['area']} / {m['source']}")
    print(f"{'='*60}")
    print(f"  OSM edges (lines)        : {m['n_edges']:,}  ({m['km_total']:,} km)")
    print(f"  Provider segments (used) : {m['n_segments']:,}")
    print(f"  Matched edges            : {m['n_matched']:,}  ({m['matched_pct']}%)")
    print(f"  Unmatched edges          : {m['n_unmatched']:,}")
    print(f"  Matched length           : {m['km_matched']:,} km  ({m['km_matched_pct']}%)")
    print(f"  Orphan segments          : {m['n_orphan_segments']:,}  ({m['orphan_pct']}%)")
    print(f"  Matched pairs (total)    : {m['n_pairs']:,}")
    print(f"  M:1 edges (≥2 segments)  : {m['n_M_to_1_edges']:,}  ({m['M_to_1_pct_of_matched']}% of matched, max {m['max_segs_per_edge']})")
    print(f"     ↳ with level conflict : {m['n_conflict_edges']:,}  ({m['conflict_pct_of_M_to_1']}% of M:1)")
    print(f"  1:N segments (≥2 edges)  : {m['n_1_to_N_segments']:,}  (max {m['max_edges_per_seg']})")
    print(f"  Overlap frac (median)    : {m['overlap_median']}")
    print(f"  Bearing diff (median)    : {m['bear_diff_median']}°")
    print(f"\n  Coverage by highway class:")
    print(rep["highway_table"][["highway", "edges", "matched", "matched_pct", "km"]]
          .to_string(index=False))


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("area", help="area name, e.g. sodermalm")
    p.add_argument("--sources", nargs="+", default=["mapbox", "tomtom"])
    args = p.parse_args()

    reps = {}
    for src in args.sources:
        rep = build_report(args.area, src)
        reps[src] = rep
        print_text_report(rep)

    if len(reps) == 2:
        a, b = args.sources[0], args.sources[1]
        cmp = compare_sources(reps[a], reps[b])
        print(f"\n{'='*60}")
        print(f"  Cross-source — {cmp['source_a']} vs {cmp['source_b']}")
        print(f"{'='*60}")
        print(f"  matched {cmp['source_a']}: {cmp['matched_a']:,}   "
              f"matched {cmp['source_b']}: {cmp['matched_b']:,}")
        print(f"  both: {cmp['both']:,}   only {cmp['source_a']}: {cmp['only_a']:,}   "
              f"only {cmp['source_b']}: {cmp['only_b']:,}")
        print(f"  agree on level (of 'both'): {cmp['agree_on_level']:,}  ({cmp['agree_pct']}%)")
