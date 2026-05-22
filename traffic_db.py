"""
traffic_db — Python library for working with an osm-traffic-enrichment DuckDB file.

Traffic congestion is stored exclusively in `edge_congestion_history` (never on
`driving.edges`). All congestion reads join with the history table to find the
latest run per source.

Usage (read-only, context manager):
    from traffic_db import TrafficDB

    with TrafficDB('db/sodermalm.duckdb') as db:
        edges   = db.get_edges(source='google', congestion='heavy')
        m       = db.plot_edges(edges)

Usage (write, e.g. from a notebook):
    with TrafficDB('db/sodermalm.duckdb', read_only=False) as db:
        run_id = db.write_congestion(edge_congestion, source='google',
                                     zoom=16, n_segments=total_pixels,
                                     boundary_name='sodermalm')

The class can also be used without a context manager; call db.close() when done.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import duckdb
import folium
import geopandas as gpd
import pandas as pd

# ── Color palette ─────────────────────────────────────────────────────────────
# Google Maps JS API TrafficLayer reference colors (googletraffic R package)
CONGESTION_COLORS: dict[str, str] = {
    "low":      "#11D68F",   # teal-green
    "moderate": "#FFCF43",   # yellow
    "heavy":    "#F24E42",   # red
    "severe":   "#A92727",   # dark red
    "no data":  "#cccccc",
}
CONGESTION_ORDER: list[str] = ["low", "moderate", "heavy", "severe", "no data"]


class TrafficDB:
    """
    Interface to an osm-traffic-enrichment DuckDB database.

    Congestion data lives only in `edge_congestion_history` and `runs`.
    The `driving.edges` table contains pure OSM network data.

    Parameters
    ----------
    db_path   : path to the .duckdb file
    read_only : True (default) for queries only; False to allow writes
    """

    def __init__(self, db_path: str | Path, read_only: bool = True) -> None:
        self.db_path   = Path(db_path)
        self.read_only = read_only
        self._con: Optional[duckdb.DuckDBPyConnection] = None

    # ── Connection management ──────────────────────────────────────────────

    def __enter__(self) -> "TrafficDB":
        self._open()
        return self

    def __exit__(self, *_) -> None:
        self.close()

    def _open(self) -> None:
        if self._con is None:
            self._con = duckdb.connect(str(self.db_path), read_only=self.read_only)
            self._con.execute("LOAD spatial")

    def close(self) -> None:
        """Close the database connection."""
        if self._con:
            self._con.close()
            self._con = None

    @property
    def con(self) -> duckdb.DuckDBPyConnection:
        """Return an open connection, opening one if necessary."""
        self._open()
        return self._con

    # ── Internal helpers ──────────────────────────────────────────────────

    @staticmethod
    def _latest_cte(source: str, run_id: Optional[int] = None) -> str:
        """
        CTE that selects congestion per edge for one source.

        run_id=None  → latest run for that source
        run_id=N     → specific historical run N
        """
        if run_id is not None:
            run_filter = f"AND r.run_id = {run_id}"
        else:
            run_filter = f"AND r.run_id = (SELECT max(run_id) FROM runs WHERE source = '{source}')"
        return (
            f"latest_{source} AS ("
            f"  SELECT h.edge_id, h.congestion, r.fetched_at, r.run_id"
            f"  FROM edge_congestion_history h"
            f"  JOIN runs r ON h.run_id = r.run_id"
            f"  WHERE r.source = '{source}' {run_filter}"
            f")"
        )

    @staticmethod
    def _to_gdf(df: pd.DataFrame, wkt_col: str = "wkt_geom",
                crs: str = "EPSG:4326") -> gpd.GeoDataFrame:
        return gpd.GeoDataFrame(
            df.drop(columns=[wkt_col]),
            geometry=gpd.GeoSeries.from_wkt(df[wkt_col]),
            crs=crs,
        )

    # ── Schema ────────────────────────────────────────────────────────────

    def list_tables(self) -> pd.DataFrame:
        """All tables in the database with schema and column count."""
        return self.con.execute("""
            SELECT table_schema AS schema,
                   table_name,
                   count(*) AS columns
            FROM information_schema.columns
            GROUP BY table_schema, table_name
            ORDER BY schema, table_name
        """).df()

    def migrate_schema(self) -> None:
        """
        Ensure runs and edge_congestion_history tables exist.
        Does NOT add any columns to driving.edges (congestion lives in history only).
        Requires read_only=False.
        """
        if self.read_only:
            raise ValueError("migrate_schema requires read_only=False")
        self.con.execute("""
            CREATE TABLE IF NOT EXISTS runs (
                run_id        INTEGER PRIMARY KEY,
                boundary_name VARCHAR,
                source        VARCHAR,
                zoom          INTEGER,
                fetched_at    TIMESTAMP,
                n_tiles       INTEGER,
                n_segments    INTEGER
            )
        """)
        self.con.execute("""
            CREATE TABLE IF NOT EXISTS edge_congestion_history (
                run_id     INTEGER,
                edge_id    INTEGER,
                source     VARCHAR,
                congestion VARCHAR,
                matched_at TIMESTAMP
            )
        """)

    # ── Edge queries ──────────────────────────────────────────────────────

    def get_history_index(self) -> pd.DataFrame:
        """
        Summary of all historical runs — which sources are available, on what dates,
        and how many edges have data. Use run_id values to query a specific snapshot.

        Returns
        -------
        DataFrame with columns:
            run_id, source, boundary_name, zoom, fetched_at, edges_with_data, n_segments
        """
        return self.con.execute("""
            SELECT r.run_id,
                   r.source,
                   r.boundary_name,
                   r.zoom,
                   r.fetched_at,
                   count(h.edge_id) AS edges_with_data,
                   r.n_segments
            FROM runs r
            LEFT JOIN edge_congestion_history h ON h.run_id = r.run_id
            GROUP BY r.run_id, r.source, r.boundary_name, r.zoom, r.fetched_at, r.n_segments
            ORDER BY r.fetched_at DESC, r.source
        """).df()

    def get_edges(
        self,
        mode:       str           = "driving",
        source:     str           = "mapbox",
        highway:    Optional[str] = None,
        congestion: Optional[str] = None,
        name:       Optional[str] = None,
        limit:      Optional[int] = None,
        run_id:     Optional[int] = None,
    ) -> gpd.GeoDataFrame:
        """
        Load road edges as a GeoDataFrame, joining congestion from history.

        Parameters
        ----------
        mode       : 'driving' | 'walking' | 'cycling'
        source     : 'mapbox' | 'google' | any source
        highway    : filter by highway type, e.g. 'primary'
        congestion : filter by level, e.g. 'heavy'
        name       : partial road name search (case-insensitive)
        limit      : maximum rows to return
        run_id     : specific historical run (None = latest run for source)
        """
        cte = self._latest_cte(source, run_id)
        # Also include the other main source for side-by-side columns
        other = "google" if source == "mapbox" else "mapbox"
        cte_other = self._latest_cte(other)

        cong_filter = f"AND coalesce(l.congestion, 'no data') = '{congestion}'" if congestion else ""
        hw_filter   = f"AND e.highway = '{highway}'" if highway else ""
        nm_filter   = f"AND lower(e.name) LIKE '%{name.lower()}%'" if name else ""
        lim         = f"LIMIT {limit}" if limit else ""

        df = self.con.execute(f"""
            WITH {cte}, {cte_other}
            SELECT e.edge_id, e.highway, e.name, e.oneway, e.length_m,
                   e.maxspeed_kmh, e.cost_s, e.from_cell, e.to_cell,
                   coalesce(l.congestion,       'no data') AS congestion,
                   coalesce(l_{other}.congestion, 'no data') AS congestion_{other},
                   l.fetched_at                            AS congestion_at,
                   ST_AsText(e.geometry) AS wkt_geom
            FROM {mode}.edges e
            LEFT JOIN latest_{source} l       ON e.edge_id = l.edge_id
            LEFT JOIN latest_{other}  l_{other} ON e.edge_id = l_{other}.edge_id
            WHERE 1=1 {hw_filter} {nm_filter} {cong_filter}
            ORDER BY e.edge_id
            {lim}
        """).df()
        # Add a consistently-named source column for callers that check congestion_mapbox / congestion_google
        df[f"congestion_{source}"] = df["congestion"]
        return self._to_gdf(df)

    def get_network_stats(self, mode: str = "driving") -> pd.DataFrame:
        """Edge count, total km, and average speed/cost per highway type."""
        return self.con.execute(f"""
            SELECT highway,
                   count(*)                       AS edges,
                   round(sum(length_m)/1000, 1)   AS km,
                   round(avg(maxspeed_kmh), 0)     AS avg_speed_kmh,
                   round(avg(cost_s), 1)           AS avg_cost_s
            FROM {mode}.edges
            GROUP BY highway
            ORDER BY edges DESC
        """).df()

    def get_nearby_edges(
        self,
        lon:      float,
        lat:      float,
        radius_m: float       = 500,
        mode:     str         = "driving",
        source:   str         = "mapbox",
        run_id:   Optional[int] = None,
    ) -> gpd.GeoDataFrame:
        """Edges within radius_m metres of (lon, lat), with congestion from history.

        run_id=None uses the latest run; pass a specific run_id for a historical snapshot.
        """
        cte = self._latest_cte(source, run_id)
        df = self.con.execute(f"""
            WITH {cte}
            SELECT e.edge_id, e.name, e.highway,
                   coalesce(l.congestion, 'no data') AS congestion,
                   e.length_m,
                   round(ST_Distance(
                       ST_Transform(e.geometry,       'EPSG:4326', 'EPSG:3857'),
                       ST_Transform(ST_Point({lon}, {lat}), 'EPSG:4326', 'EPSG:3857')
                   )) AS dist_m,
                   ST_AsText(e.geometry) AS wkt_geom
            FROM {mode}.edges e
            LEFT JOIN latest_{source} l ON e.edge_id = l.edge_id
            WHERE ST_Distance(
                ST_Transform(e.geometry,       'EPSG:4326', 'EPSG:3857'),
                ST_Transform(ST_Point({lon}, {lat}), 'EPSG:4326', 'EPSG:3857')
            ) <= {radius_m}
            ORDER BY dist_m
        """).df()
        return self._to_gdf(df)

    # ── Congestion queries ────────────────────────────────────────────────

    def get_congestion_summary(
        self,
        source: str         = "mapbox",
        mode:   str         = "driving",
        run_id: Optional[int] = None,
    ) -> pd.DataFrame:
        """Edge count and km per congestion level for the chosen source.

        run_id=None uses the latest run; pass a specific run_id for a historical snapshot.
        """
        cte = self._latest_cte(source, run_id)
        return self.con.execute(f"""
            WITH {cte}
            SELECT coalesce(l.congestion, 'no data')          AS congestion,
                   count(*)                                    AS edges,
                   round(sum(e.length_m)/1000, 1)             AS km,
                   round(count(*)*100.0 / sum(count(*)) OVER (), 1) AS pct
            FROM {mode}.edges e
            LEFT JOIN latest_{source} l ON e.edge_id = l.edge_id
            GROUP BY l.congestion
            ORDER BY km DESC
        """).df()

    def get_runs(self) -> pd.DataFrame:
        """All pipeline runs with source, zoom, timestamp, and edge match counts."""
        return self.con.execute("""
            SELECT r.run_id,
                   coalesce(r.source, '?')  AS source,
                   r.boundary_name,
                   r.zoom,
                   r.fetched_at,
                   r.n_tiles,
                   r.n_segments,
                   count(h.edge_id)         AS edges_matched
            FROM runs r
            LEFT JOIN edge_congestion_history h ON h.run_id = r.run_id
            GROUP BY 1, 2, 3, 4, 5, 6, 7
            ORDER BY r.fetched_at
        """).df()

    def get_congestion_history(
        self,
        edge_id:   Optional[int] = None,
        road_name: Optional[str] = None,
        source:    Optional[str] = None,
        limit:     int           = 500,
    ) -> pd.DataFrame:
        """
        Time-series congestion records. Provide either edge_id or road_name.

        Parameters
        ----------
        edge_id   : exact edge ID
        road_name : partial road name match (case-insensitive)
        source    : 'mapbox' | 'google' | None (both)
        limit     : maximum rows (default 500)
        """
        if edge_id is not None:
            edge_filter = f"h.edge_id = {edge_id}"
        elif road_name:
            edge_filter = f"lower(e.name) LIKE '%{road_name.lower()}%'"
        else:
            raise ValueError("Supply either edge_id or road_name")
        src_filter = f"AND h.source = '{source}'" if source else ""

        return self.con.execute(f"""
            SELECT r.run_id,
                   r.fetched_at,
                   coalesce(h.source, r.source, '?') AS source,
                   h.edge_id,
                   e.name,
                   e.highway,
                   h.congestion,
                   h.matched_at
            FROM edge_congestion_history h
            JOIN runs r          ON h.run_id  = r.run_id
            JOIN driving.edges e ON h.edge_id = e.edge_id
            WHERE {edge_filter} {src_filter}
            ORDER BY r.fetched_at, h.source, h.edge_id
            LIMIT {limit}
        """).df()

    def get_congestion_comparison(
        self,
        mode:           str         = "driving",
        mapbox_run_id:  Optional[int] = None,
        google_run_id:  Optional[int] = None,
    ) -> pd.DataFrame:
        """
        All edges with congestion from both sources plus an agreement category.
        Reads from edge_congestion_history (not from driving.edges columns).

        Parameters
        ----------
        mapbox_run_id : specific Mapbox run (None = latest)
        google_run_id : specific Google run (None = latest)
        """
        cte_mb = self._latest_cte("mapbox", mapbox_run_id)
        cte_gg = self._latest_cte("google", google_run_id)
        df = self.con.execute(f"""
            WITH {cte_mb}, {cte_gg}
            SELECT e.edge_id, e.highway, e.name, e.length_m,
                   coalesce(lm.congestion, 'no data') AS congestion_mapbox,
                   coalesce(lg.congestion, 'no data') AS congestion_google,
                   strftime(lm.fetched_at, '%Y-%m-%d %H:%M') AS mapbox_at,
                   strftime(lg.fetched_at, '%Y-%m-%d %H:%M') AS google_at,
                   ST_AsText(e.geometry) AS wkt_geom
            FROM {mode}.edges e
            LEFT JOIN latest_mapbox lm ON e.edge_id = lm.edge_id
            LEFT JOIN latest_google lg ON e.edge_id = lg.edge_id
        """).df()

        gdf = self._to_gdf(df)

        mb  = gdf["congestion_mapbox"]
        gg  = gdf["congestion_google"]
        cat = pd.Series("both no data", index=gdf.index)
        cat[(mb == "low")  & (gg == "no data")] = "Mapbox=low, Google=no data (normal flow)"
        cat[(mb != "no data") & (gg != "no data") & (mb == gg)]              = "both agree (non-low)"
        cat[(mb.isin(["moderate","heavy","severe"])) & (gg == "no data")]    = "Mapbox congested, Google=no data"
        cat[(mb != "no data") & (gg != "no data") & (mb != gg)]              = "sources disagree"
        cat[(mb == "no data") & (gg != "no data")]                           = "Google only"
        gdf["agreement"] = cat

        return gdf

    # ── Write operations ──────────────────────────────────────────────────

    def write_congestion(
        self,
        edge_congestion: dict,
        source:          str,
        zoom:            int,
        n_segments:      int,
        boundary_name:   str = "",
    ) -> int:
        """
        Write congestion results to edge_congestion_history and runs.
        Does NOT modify driving.edges.

        Parameters
        ----------
        edge_congestion : dict mapping edge_id (int) → level ('low'|'moderate'|'heavy'|'severe')
        source          : 'google', 'mapbox', 'tomtom', or any string
        zoom            : zoom level used when fetching the data
        n_segments      : total traffic pixels (Google) or segments (Mapbox/TomTom)
        boundary_name   : area name stored in the runs table

        Returns
        -------
        run_id : int — the new run ID
        """
        if self.read_only:
            raise ValueError("write_congestion requires read_only=False")

        fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        self.migrate_schema()

        run_id = self.con.execute(
            "SELECT coalesce(max(run_id), 0) + 1 FROM runs"
        ).fetchone()[0]

        self.con.execute(
            "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?)",
            [run_id, boundary_name, source, zoom, fetched_at, 1, n_segments],
        )

        if edge_congestion:
            self.con.executemany(
                "INSERT INTO edge_congestion_history VALUES (?, ?, ?, ?, ?)",
                [
                    (run_id, int(eid), source, cong, fetched_at)
                    for eid, cong in edge_congestion.items()
                ],
            )

        return run_id

    # ── Visualization ─────────────────────────────────────────────────────

    def plot_edges(
        self,
        gdf:       gpd.GeoDataFrame,
        color_col: str = "congestion",
        zoom:      int = 14,
        tiles:     str = "OpenStreetMap",
    ) -> folium.Map:
        """
        Folium map of edges colored by congestion level.

        Parameters
        ----------
        gdf       : GeoDataFrame returned by get_edges()
        color_col : column to use for coloring (default 'congestion')
        zoom      : initial map zoom level
        tiles     : folium tile layer name
        """
        lines = gdf[gdf.geometry.geom_type.isin(["LineString", "MultiLineString"])].copy()
        if lines.empty:
            print("No line geometries to plot.")
            return None

        ts_cols = [c for c in lines.columns if pd.api.types.is_datetime64_any_dtype(lines[c])]
        lines   = lines.drop(columns=ts_cols, errors="ignore")

        bds    = lines.total_bounds
        center = [(bds[1] + bds[3]) / 2, (bds[0] + bds[2]) / 2]
        m      = folium.Map(location=center, zoom_start=zoom, tiles=tiles)
        folium.GeoJson(
            lines.__geo_interface__,
            style_function=lambda feat: {
                "color": CONGESTION_COLORS.get(
                    feat["properties"].get(color_col, "no data"), "#cccccc"
                ),
                "weight": 3 if feat["properties"].get(color_col, "no data") != "no data" else 1.5,
            },
            tooltip=None,
            popup=None,
        ).add_to(m)
        return m

    def plot_comparison(self, mode: str = "driving", zoom: int = 13) -> None:
        """
        Side-by-side Mapbox vs Google folium maps. Renders in Jupyter notebooks.
        Reads latest congestion from edge_congestion_history.
        """
        from html import escape
        from IPython.display import HTML, display

        gdf = self.get_congestion_comparison(mode=mode)
        viz = gdf[gdf.geometry.geom_type.isin(["LineString", "MultiLineString"])].copy()
        bds    = viz.total_bounds
        center = [(bds[1] + bds[3]) / 2, (bds[0] + bds[2]) / 2]

        def _make_map(col: str) -> str:
            m = folium.Map(location=center, zoom_start=zoom, tiles="OpenStreetMap")
            folium.GeoJson(
                viz.__geo_interface__,
                style_function=lambda feat, c=col: {
                    "color": CONGESTION_COLORS.get(
                        feat["properties"].get(c, "no data"), "#cccccc"
                    ),
                    "weight": 3 if feat["properties"].get(c, "no data") != "no data" else 1,
                },
                tooltip=folium.GeoJsonTooltip(
                    fields=[col, "highway", "name"],
                    aliases=["Congestion", "Type", "Name"],
                ),
                popup=None,
            ).add_to(m)
            return escape(m.get_root().render())

        display(HTML(f"""
        <div style="display:flex;gap:8px">
          <div style="flex:1">
            <h4 style="text-align:center;margin:4px 0">Mapbox</h4>
            <iframe srcdoc="{_make_map('congestion_mapbox')}"
                    width="100%" height="460" frameborder="0"></iframe>
          </div>
          <div style="flex:1">
            <h4 style="text-align:center;margin:4px 0">Google Maps</h4>
            <iframe srcdoc="{_make_map('congestion_google')}"
                    width="100%" height="460" frameborder="0"></iframe>
          </div>
        </div>
        """))
