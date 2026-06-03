"""
viz_flow — interactive map of the Stockholm motor-flow network (the SOURCE data).

Loads `stockholm_motor_flow` (Stockholm Trafikkontoret CC0 "Trafikflöde Motorfordon",
weekday avg daily traffic 2014-2015) from the fetching-sweden-data DuckDB and renders it as a
self-contained folium HTML, coloured + sized by `total_flow_all_directions`, with the OSM
driving grid as a toggleable grey reference layer so we can see how the flow lines sit on the
street network before deciding the matching configuration.

    conda run -n osm-traffic-enrichment python scripts/viz_flow.py
"""
from __future__ import annotations

from pathlib import Path

import duckdb
import geopandas as gpd

import roadstyle

SRC_DB = Path("/home/kaveh/projects/fetching-sweden-data/data/processed/sodermalm.duckdb")
OSM_DB = Path("/home/kaveh/projects/osm-traffic-enrichment/db/sodermalm.duckdb")
OUT_HTML = Path("/home/kaveh/projects/osm-traffic-enrichment/output/sodermalm_motor_flow.html")


def _read_wkt(db: Path, sql: str) -> gpd.GeoDataFrame:
    """Run a read-only DuckDB query whose last column is ST_AsText(...) WKT → GeoDataFrame (4326)."""
    con = duckdb.connect(str(db), read_only=True)
    con.execute("INSTALL spatial")
    con.execute("LOAD spatial")
    df = con.execute(sql).df()
    con.close()
    g = gpd.GeoDataFrame(df.drop(columns="w"), geometry=gpd.GeoSeries.from_wkt(df["w"]),
                         crs="EPSG:4326")
    return g[g.geometry.geom_type.isin(["LineString", "MultiLineString"])]


def load_flow() -> gpd.GeoDataFrame:
    return _read_wkt(SRC_DB, """
        SELECT street_name, network_type,
               flow_all_vehicles,                 -- per-geometry (per-carriageway) volume
               total_flow_all_directions,         -- corridor aggregate (denormalized; do-not-sum)
               heavy_vehicle_share, data_collection_method,
               ST_AsText(geom) AS w
        FROM stockholm_motor_flow
    """)


def load_osm_edges() -> gpd.GeoDataFrame | None:
    try:
        return _read_wkt(OSM_DB, """
            SELECT edge_id, highway, ST_AsText(geometry) AS w
            FROM driving.edges WHERE NOT is_reverse
        """)
    except Exception as e:  # OSM underlay is optional context, never block the flow map
        print(f"  (skipping OSM underlay: {e})")
        return None


def _add_osm_underlay(m, osm: gpd.GeoDataFrame) -> None:
    """Add the OSM driving grid as a thin, muted, toggleable reference layer."""
    import folium

    fg = folium.FeatureGroup(name="OSM driving grid", show=True)
    folium.GeoJson(
        osm.to_json(),
        style_function=lambda _f: {"color": "#7a7a7a", "weight": 1, "opacity": 0.45},
    ).add_to(fg)
    fg.add_to(m)
    folium.LayerControl(collapsed=True).add_to(m)


def main() -> None:
    flow = load_flow()
    # Colour by the PER-GEOMETRY directional volume so divided carriageways (e.g.
    # Söderledstunneln) show their true ~half each, not the doubled corridor total.
    vol = flow["flow_all_vehicles"].dropna()
    vmax = float(vol.quantile(0.95)) if len(vol) else 1.0

    m = roadstyle.render_edges(
        flow,
        backend="folium",
        theme="dark",
        color_by="flow_all_vehicles",
        cmap="plasma",
        vmin=0,
        vmax=vmax,
        width_by=(1.5, 9.0),
        tooltip=["street_name", "flow_all_vehicles", "total_flow_all_directions",
                 "heavy_vehicle_share", "data_collection_method"],
    )

    osm = load_osm_edges()
    if osm is not None:
        _add_osm_underlay(m, osm)

    OUT_HTML.parent.mkdir(parents=True, exist_ok=True)
    m.save(str(OUT_HTML))

    # ── stats ──
    measured = (flow["data_collection_method"] == "Mätt").sum()
    divided = (flow["flow_all_vehicles"] < flow["total_flow_all_directions"]).sum()
    print(f"\nWrote {OUT_HTML}")
    print(f"  segments              : {len(flow):,}")
    print(f"  distinct streets      : {flow['street_name'].nunique():,}")
    print(f"  flow_all_vehicles     : min {vol.min():,.0f} | median {vol.median():,.0f} "
          f"| p95 {vmax:,.0f} | max {vol.max():,.0f}")
    print(f"  divided-carriageway   : {divided:,} segs (dir < corridor total)")
    print(f"  measured / estimated  : {measured:,} / {len(flow) - measured:,}")
    if osm is not None:
        print(f"  OSM underlay edges  : {len(osm):,}")


if __name__ == "__main__":
    main()
