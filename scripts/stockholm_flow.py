"""
stockholm_flow — match OSM driving edges onto Stockholm motor-flow (OSM -> flow).

See docs/stockholm_flow_matching.md. Steps:
  1. MATCH    run_match_routes(flow, osm) -> routes_summary (route-level per OSM edge) and
              routes_long (one row per OSM edge x flow segment).
  2. FILTER   per twin pair (same osm_id + unordered node pair), drop the edge with the lower
              ROUTE-LEVEL quality (from routes_summary). Survivors = the "remaining edges".
  3. RESOLVE  for the remaining edges, in routes_long: source_flow_id = the flow segment covering
              the most of the edge; covering_match = edge_cover_pct; quality_match = edge-to-edge
              score, both for that edge<->flow pair.
  4. EXTEND   copy the survivor's match to its dropped twin (match_source: best | extended).

Output `edge_stockholm_flow_match` (the match only): edge_id, is_reverse, source_flow_id,
covering_match, quality_match, match_source, matched_at. Join source_flow_id ->
stockholm_motor_flow for any flow attribute values.

    conda run -n osm-traffic-enrichment python scripts/stockholm_flow.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import geopandas as gpd
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # project root -> `scripts` pkg
from scripts.route_match import run_match_routes  # noqa: E402

SRC_DB = Path("/home/kaveh/projects/fetching-sweden-data/data/processed/sodermalm.duckdb")
OSM_DB = Path("/home/kaveh/projects/osm-traffic-enrichment/db/sodermalm.duckdb")
OUT_TABLE = "edge_stockholm_flow_match"
TAU = 15.0   # metres; drift scale in the quality score (network_matching convention)


def _read_wkt(db: Path, sql: str) -> gpd.GeoDataFrame:
    c = duckdb.connect(str(db), read_only=True)
    c.execute("INSTALL spatial"); c.execute("LOAD spatial")
    df = c.execute(sql).df(); c.close()
    g = gpd.GeoDataFrame(df.drop(columns="w"), geometry=gpd.GeoSeries.from_wkt(df["w"]),
                         crs="EPSG:4326")
    return g[g.geometry.geom_type.isin(["LineString", "MultiLineString"])]


def _quality(dtw, bearing):
    """100·exp(-dtw/TAU)·max(0,cos(bearing)); 0 where unmatched (dtw is NaN)."""
    dtw = np.asarray(dtw, float); bearing = np.asarray(bearing, float)
    q = 100.0 * np.exp(-dtw / TAU) * np.clip(np.cos(np.radians(bearing)), 0, None)
    return np.where(np.isnan(dtw), 0.0, q)


# ── Step 1 — match; route-level quality (routes_summary) + edge-to-edge best match (routes_long) ──
def match_scored() -> tuple[pd.DataFrame, dict, dict]:
    """Run network_matching OSM->flow. Returns (scored, route_quality, report).

    scored        : per matched OSM edge, its best-cover flow from `routes_long` (edge-to-edge) —
                    columns `source_flow_id`, `covering_match`, `quality_match`; indexed by edge_id.
    route_quality : {osm edge_id -> route-level quality from `routes_summary`} (NO_MATCH -> 0), used
                    only to pick the surviving twin.
    """
    flow = _read_wkt(SRC_DB, "SELECT id AS source_flow_id, ST_AsText(ST_Force2D(geom)) AS w "
                             "FROM stockholm_motor_flow").reset_index(drop=True)
    flow["edge_id"] = range(len(flow))                    # network_matching B needs 'edge_id'
    flow_attr = flow.drop(columns="geometry").rename(columns={"edge_id": "flow_eid"})

    osm = _read_wkt(OSM_DB, "SELECT edge_id AS osm_edge_id, ST_AsText(ST_Force2D(geometry)) AS w "
                            "FROM driving.edges")
    osm["level"] = "x"

    rs, rl, seg = run_match_routes(flow, osm, utm_srid=3006, level_col="level",
                                   extra_cols=("osm_edge_id",))
    s2o = seg[["seg_id", "osm_edge_id"]]

    # routes_summary -> route-level quality per OSM edge (for the twin filter)
    rs = rs.merge(s2o, left_on="source_id", right_on="seg_id", how="left")
    route_quality = dict(zip(rs["osm_edge_id"].astype(int),
                             _quality(rs["dtw_distance"], rs["bearing_diff"]).round(1)))

    # routes_long -> per OSM edge, the flow segment covering the most of it (edge-to-edge scores)
    rl = rl.merge(s2o, left_on="source_id", right_on="seg_id", how="left")
    rl["covering_match"] = rl["edge_cover_pct"].round(1)
    rl["quality_match"] = _quality(rl["edge_match_dist_avg"], rl["edge_bearing_diff"]).round(1)
    best = rl.loc[rl.groupby("osm_edge_id")["edge_cover_pct"].idxmax()].copy()
    best = best.merge(flow_attr[["flow_eid", "source_flow_id"]],
                      left_on="dest_id", right_on="flow_eid", how="left")
    scored = best.set_index("osm_edge_id")[["source_flow_id", "covering_match", "quality_match"]]

    report = {"flow_segments": int(len(flow)), "osm_edges": int(len(osm)),
              "edges_matched_raw": int(len(scored))}
    return scored, route_quality, report


# ── Steps 2-4 — twin filter (routes_summary), then extend the survivor's edge-to-edge match ──
def resolve_twins(scored: pd.DataFrame, route_quality: dict) -> pd.DataFrame:
    """Per (osm_id, unordered node pair): keep the higher route-quality twin, copy its match to both."""
    con = duckdb.connect(str(OSM_DB), read_only=True)
    ek = con.execute("SELECT edge_id, is_reverse, osm_id, "
                     "least(source, target) AS a, greatest(source, target) AS b "
                     "FROM driving.edges").df()
    con.close()

    out = []
    for (_osm, _a, _b), grp in ek.groupby(["osm_id", "a", "b"]):
        rev = grp.set_index("edge_id")["is_reverse"]
        ids = [int(e) for e in grp["edge_id"]]
        winner = max(ids, key=lambda e: route_quality.get(e, 0.0))   # routes_summary route quality
        if route_quality.get(winner, 0.0) <= 0 or winner not in scored.index:
            continue                                                 # nobody in this group matched
        w = scored.loc[winner]                                       # edge-to-edge match (routes_long)
        for eid in ids:
            out.append({
                "edge_id": eid, "is_reverse": bool(rev.loc[eid]),
                "source_flow_id": w["source_flow_id"],
                "covering_match": w["covering_match"],
                "quality_match": w["quality_match"],
                "match_source": "best" if eid == winner else "extended",
            })
    return pd.DataFrame(out)


def write_match(df: pd.DataFrame, db: Path = OSM_DB, table: str = OUT_TABLE) -> int:
    df = df.copy()
    df["matched_at"] = datetime.now(timezone.utc).replace(tzinfo=None)
    con = duckdb.connect(str(db), read_only=False)
    con.execute("INSTALL spatial"); con.execute("LOAD spatial")
    con.register("match_df", df)
    con.execute(f"CREATE OR REPLACE TABLE {table} AS SELECT * FROM match_df")
    con.unregister("match_df")
    n = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    con.close()
    return n


def run() -> tuple[pd.DataFrame, dict]:
    scored, route_quality, report = match_scored()
    final = resolve_twins(scored, route_quality)
    report["edges_enriched"] = int(len(final))
    report["best"] = int((final["match_source"] == "best").sum())
    report["extended"] = int((final["match_source"] == "extended").sum())
    return final, report


def format_report(report: dict) -> list[str]:
    pct = 100 * report["edges_enriched"] / report["osm_edges"] if report["osm_edges"] else 0
    return [
        "  Match (OSM -> flow; routes_summary twin filter -> routes_long extend):",
        f"    flow segments        : {report['flow_segments']:,}",
        f"    OSM edges            : {report['osm_edges']:,}",
        f"    matched (raw)        : {report['edges_matched_raw']:,}",
        f"    enriched (best+ext)  : {report['edges_enriched']:,} ({pct:.0f}%)",
        f"      best / extended    : {report['best']:,} / {report['extended']:,}",
    ]


def main() -> None:
    final, report = run()
    print("\n".join(format_report(report)))
    n = write_match(final)
    print(f"\nWrote table {OUT_TABLE}: {n:,} rows")


if __name__ == "__main__":
    main()
