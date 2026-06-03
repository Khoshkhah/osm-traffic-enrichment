"""
traffic_db — Python library for working with an osm-traffic-enrichment DuckDB file.

Each traffic source has its own history table:
  mapbox_congestion_history (run_id, edge_id, congestion, matched_at)
  google_congestion_history (run_id, edge_id, congestion, matched_at)
  tomtom_congestion_history (run_id, edge_id, traffic_level, congestion, matched_at)

Usage (read-only, context manager):
    import sys; sys.path.insert(0, '..')
    from scripts.traffic_db import TrafficDB

    with TrafficDB('db/sodermalm.duckdb') as db:
        edges = db.get_edges(source='mapbox', congestion='heavy')
        m     = db.plot_edges(edges)

Usage (write, e.g. from a notebook):
    with TrafficDB('db/sodermalm.duckdb', read_only=False) as db:
        run_id = db.write_congestion(edge_congestion, source='mapbox',
                                     zoom=14, n_segments=total,
                                     boundary_name='sodermalm')
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import duckdb
import folium
import geopandas as gpd
import pandas as pd

CONGESTION_COLORS: dict[str, str] = {
    "low":      "#11D68F",
    "moderate": "#FFCF43",
    "heavy":    "#F24E42",
    "severe":   "#A92727",
    "no data":  "#cccccc",
}
CONGESTION_ORDER: list[str] = ["low", "moderate", "heavy", "severe", "no data"]

_TOMTOM_EXTRA_COL = "traffic_level"


class TrafficDB:
    """
    Interface to an osm-traffic-enrichment DuckDB database.

    Each source has a separate history table ({source}_congestion_history).
    The driving.edges table contains pure OSM network data.

    Parameters
    ----------
    db_path   : path to the .duckdb file
    read_only : True (default) for queries only; False to allow writes
    """

    def __init__(self, db_path: str | Path, read_only: bool = True) -> None:
        self.db_path   = Path(db_path)
        self.read_only = read_only
        self._con: Optional[duckdb.DuckDBPyConnection] = None

    def __enter__(self) -> "TrafficDB":
        self._open()
        return self

    def __exit__(self, *_) -> None:
        self.close()

    def _open(self) -> None:
        if self._con is None:
            self._con = duckdb.connect(str(self.db_path), read_only=self.read_only)
            self._con.execute("INSTALL spatial")
            self._con.execute("LOAD spatial")

    def close(self) -> None:
        if self._con:
            self._con.close()
            self._con = None

    @property
    def con(self) -> duckdb.DuckDBPyConnection:
        self._open()
        return self._con

    # ── Internal helpers ──────────────────────────────────────────────────

    @staticmethod
    def _latest_cte(source: str, run_id: Optional[int] = None) -> str:
        """CTE that selects the latest congestion per edge for one source."""
        table = f"{source}_congestion_history"
        if run_id is not None:
            run_clause = f"AND r.run_id = {run_id}"
        else:
            run_clause = f"AND r.run_id = (SELECT max(run_id) FROM runs WHERE source = '{source}')"
        return (
            f"latest_{source} AS ("
            f"  SELECT h.edge_id, h.congestion, r.fetched_at, r.run_id"
            f"  FROM {table} h"
            f"  JOIN runs r ON h.run_id = r.run_id"
            f"  WHERE 1=1 {run_clause}"
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

    def _history_table_exists(self, source: str) -> bool:
        n = self.con.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_name = ?",
            [f"{source}_congestion_history"],
        ).fetchone()[0]
        return n > 0

    # ── Schema ────────────────────────────────────────────────────────────

    def list_tables(self) -> pd.DataFrame:
        return self.con.execute("""
            SELECT table_schema AS schema,
                   table_name,
                   count(*) AS columns
            FROM information_schema.columns
            GROUP BY table_schema, table_name
            ORDER BY schema, table_name
        """).df()

    def migrate_schema(self, source: str = None) -> None:
        """
        Ensure runs table and optionally a source-specific history table exist.
        Migrates data from legacy edge_congestion_history if present.
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

        if source is None:
            return

        if source == "tomtom":
            self.con.execute(f"""
                CREATE TABLE IF NOT EXISTS tomtom_congestion_history (
                    run_id         INTEGER,
                    edge_id        INTEGER,
                    traffic_level  DOUBLE,
                    congestion     VARCHAR,
                    covering_match INTEGER,
                    quality_match  INTEGER,
                    matched_at     TIMESTAMP
                )
            """)
        else:
            self.con.execute(f"""
                CREATE TABLE IF NOT EXISTS {source}_congestion_history (
                    run_id         INTEGER,
                    edge_id        INTEGER,
                    congestion     VARCHAR,
                    covering_match INTEGER,
                    quality_match  INTEGER,
                    matched_at     TIMESTAMP
                )
            """)
        # Add the match-quality columns to any pre-existing (older-schema) table.
        tbl = f"{source}_congestion_history"
        for col in ("covering_match", "quality_match"):
            self.con.execute(f"ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS {col} INTEGER")

        self._migrate_from_legacy(source)

    def _migrate_from_legacy(self, source: str) -> None:
        """Copy rows from old shared edge_congestion_history into source-specific table."""
        legacy_exists = self.con.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_name = 'edge_congestion_history'"
        ).fetchone()[0]
        if not legacy_exists:
            return

        target = f"{source}_congestion_history"
        already = self.con.execute(f"SELECT count(*) FROM {target}").fetchone()[0]
        if already > 0:
            return

        n = self.con.execute(
            "SELECT count(*) FROM edge_congestion_history WHERE source = ?", [source]
        ).fetchone()[0]
        if n == 0:
            return

        if source == "tomtom":
            self.con.execute(f"""
                INSERT INTO {target} (run_id, edge_id, traffic_level, congestion, matched_at)
                SELECT run_id, edge_id, NULL, congestion, matched_at
                FROM edge_congestion_history WHERE source = '{source}'
            """)
        else:
            self.con.execute(f"""
                INSERT INTO {target} (run_id, edge_id, congestion, matched_at)
                SELECT run_id, edge_id, congestion, matched_at
                FROM edge_congestion_history WHERE source = '{source}'
            """)

    # ── History queries ───────────────────────────────────────────────────

    def get_history_index(self) -> pd.DataFrame:
        """All historical runs with edge counts per run."""
        runs = self.con.execute("""
            SELECT run_id, source, boundary_name, zoom, fetched_at, n_segments
            FROM runs ORDER BY fetched_at DESC, source
        """).df()

        if runs.empty:
            return pd.DataFrame(columns=[
                "run_id", "source", "boundary_name", "zoom",
                "fetched_at", "edges_with_data", "n_segments",
            ])

        results = []
        for _, row in runs.iterrows():
            source = row["source"] or ""
            table  = f"{source}_congestion_history"
            try:
                count = self.con.execute(
                    f"SELECT count(*) FROM {table} WHERE run_id = {row['run_id']}"
                ).fetchone()[0]
            except Exception:
                count = 0
            results.append({**row.to_dict(), "edges_with_data": count})

        df = pd.DataFrame(results)
        cols = ["run_id", "source", "boundary_name", "zoom",
                "fetched_at", "edges_with_data", "n_segments"]
        return df[cols]

    def get_runs(self) -> pd.DataFrame:
        """All pipeline runs — alias for get_history_index()."""
        return self.get_history_index()

    # ── Edge queries ──────────────────────────────────────────────────────

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
        """Load road edges with congestion from {source}_congestion_history."""
        if not self._history_table_exists(source):
            raise ValueError(
                f"No history table for source '{source}'. "
                f"Run a fetch notebook first to populate {source}_congestion_history."
            )

        cte          = self._latest_cte(source, run_id)
        cong_filter  = f"AND coalesce(l.congestion, 'no data') = '{congestion}'" if congestion else ""
        hw_filter    = f"AND e.highway = '{highway}'" if highway else ""
        nm_filter    = f"AND lower(e.name) LIKE '%{name.lower()}%'" if name else ""
        lim          = f"LIMIT {limit}" if limit else ""

        df = self.con.execute(f"""
            WITH {cte}
            SELECT e.edge_id, e.highway, e.name, e.oneway, e.length_m,
                   e.maxspeed_kmh, e.cost_s, e.from_cell, e.to_cell,
                   coalesce(l.congestion, 'no data') AS congestion,
                   l.fetched_at                      AS congestion_at,
                   ST_AsText(e.geometry)             AS wkt_geom
            FROM {mode}.edges e
            LEFT JOIN latest_{source} l ON e.edge_id = l.edge_id
            WHERE 1=1 {hw_filter} {nm_filter} {cong_filter}
            ORDER BY e.edge_id
            {lim}
        """).df()

        df[f"congestion_{source}"] = df["congestion"]
        return self._to_gdf(df)

    def get_network_stats(self, mode: str = "driving") -> pd.DataFrame:
        return self.con.execute(f"""
            SELECT highway,
                   count(*)                       AS edges,
                   round(sum(length_m)/1000, 1)   AS km,
                   round(avg(maxspeed_kmh), 0)    AS avg_speed_kmh,
                   round(avg(cost_s), 1)          AS avg_cost_s
            FROM {mode}.edges
            GROUP BY highway ORDER BY edges DESC
        """).df()

    def get_nearby_edges(
        self,
        lon:      float,
        lat:      float,
        radius_m: float         = 500,
        mode:     str           = "driving",
        source:   str           = "mapbox",
        run_id:   Optional[int] = None,
    ) -> gpd.GeoDataFrame:
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
        source: str           = "mapbox",
        mode:   str           = "driving",
        run_id: Optional[int] = None,
    ) -> pd.DataFrame:
        if not self._history_table_exists(source):
            return pd.DataFrame(columns=["congestion", "edges", "km", "pct"])

        cte = self._latest_cte(source, run_id)
        return self.con.execute(f"""
            WITH {cte}
            SELECT coalesce(l.congestion, 'no data')             AS congestion,
                   count(*)                                      AS edges,
                   round(sum(e.length_m)/1000, 1)               AS km,
                   round(count(*)*100.0 / sum(count(*)) OVER (), 1) AS pct
            FROM {mode}.edges e
            LEFT JOIN latest_{source} l ON e.edge_id = l.edge_id
            GROUP BY l.congestion
            ORDER BY km DESC
        """).df()

    def get_congestion_history(
        self,
        edge_id:   Optional[int] = None,
        road_name: Optional[str] = None,
        source:    str           = None,
        limit:     int           = 500,
    ) -> pd.DataFrame:
        """
        Time-series congestion records for a specific edge or road.

        Parameters
        ----------
        edge_id   : exact edge ID
        road_name : partial road name match (case-insensitive)
        source    : required — 'mapbox', 'google', or 'tomtom'
        limit     : max rows
        """
        if source is None:
            raise ValueError("source is required: 'mapbox', 'google', or 'tomtom'")
        if not self._history_table_exists(source):
            return pd.DataFrame()

        if edge_id is not None:
            edge_filter = f"h.edge_id = {edge_id}"
        elif road_name:
            edge_filter = f"lower(e.name) LIKE '%{road_name.lower()}%'"
        else:
            raise ValueError("Supply either edge_id or road_name")

        table = f"{source}_congestion_history"
        extra = "h.traffic_level," if source == "tomtom" else ""

        return self.con.execute(f"""
            SELECT r.run_id, r.fetched_at, '{source}' AS source,
                   h.edge_id, e.name, e.highway,
                   {extra}
                   h.congestion, h.matched_at
            FROM {table} h
            JOIN runs r          ON h.run_id  = r.run_id
            JOIN driving.edges e ON h.edge_id = e.edge_id
            WHERE {edge_filter}
            ORDER BY r.fetched_at, h.edge_id
            LIMIT {limit}
        """).df()

    def get_congestion_comparison(
        self,
        mode:    str           = "driving",
        sources: list[str]     = None,
        run_ids: dict          = None,
    ) -> gpd.GeoDataFrame:
        """
        All edges with congestion from multiple sources.

        Parameters
        ----------
        sources  : list of sources to compare (default: ['mapbox', 'google'])
        run_ids  : dict of source → run_id (optional, None = latest per source)
        """
        if sources is None:
            sources = ["mapbox", "google"]
        if run_ids is None:
            run_ids = {}

        available = [s for s in sources if self._history_table_exists(s)]
        if not available:
            return gpd.GeoDataFrame()

        ctes       = [self._latest_cte(s, run_ids.get(s)) for s in available]
        select_cols = ", ".join(
            f"coalesce(l_{s}.congestion, 'no data') AS congestion_{s}"
            for s in available
        )
        joins = "\n".join(
            f"LEFT JOIN latest_{s} l_{s} ON e.edge_id = l_{s}.edge_id"
            for s in available
        )

        df = self.con.execute(f"""
            WITH {', '.join(ctes)}
            SELECT e.edge_id, e.highway, e.name, e.length_m,
                   {select_cols},
                   ST_AsText(e.geometry) AS wkt_geom
            FROM {mode}.edges e
            {joins}
        """).df()

        return self._to_gdf(df)

    # ── Write operations ──────────────────────────────────────────────────

    def write_congestion(
        self,
        edge_congestion: dict,
        source:          str,
        zoom:            int,
        n_segments:      int,
        n_tiles:         int            = 1,
        boundary_name:   str            = "",
        traffic_levels:  Optional[dict] = None,
        covering:        Optional[dict] = None,
        quality:         Optional[dict] = None,
    ) -> int:
        """
        Write congestion results to {source}_congestion_history and runs.

        Parameters
        ----------
        edge_congestion : dict mapping edge_id → 'low'|'moderate'|'heavy'|'severe'
        source          : 'mapbox', 'google', 'tomtom', or any string
        zoom            : zoom level used when fetching
        n_segments      : total traffic pixels (Google) or segments (Mapbox/TomTom)
        n_tiles         : count of map tiles covering the boundary at `zoom`
        boundary_name   : area name stored in runs
        traffic_levels  : optional dict edge_id → float (TomTom raw traffic_level only)
        covering        : optional dict edge_id → covering_match (integer 0–100, % of edge covered)
        quality         : optional dict edge_id → quality_match (integer 0–100, 100·alignment)

        Returns
        -------
        run_id : int
        """
        if self.read_only:
            raise ValueError("write_congestion requires read_only=False")

        fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        self.migrate_schema(source)

        run_id = self.con.execute(
            "SELECT coalesce(max(run_id), 0) + 1 FROM runs"
        ).fetchone()[0]

        self.con.execute(
            "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?)",
            [run_id, boundary_name, source, zoom, fetched_at, n_tiles, n_segments],
        )

        if edge_congestion:
            table = f"{source}_congestion_history"
            cov, qual = covering or {}, quality or {}
            if source == "tomtom":
                self.con.executemany(
                    f"INSERT INTO {table} "
                    f"(run_id, edge_id, traffic_level, congestion, covering_match, quality_match, matched_at) "
                    f"VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [
                        (run_id, int(eid), traffic_levels.get(eid) if traffic_levels else None,
                         cong, cov.get(eid), qual.get(eid), fetched_at)
                        for eid, cong in edge_congestion.items()
                    ],
                )
            else:
                self.con.executemany(
                    f"INSERT INTO {table} "
                    f"(run_id, edge_id, congestion, covering_match, quality_match, matched_at) "
                    f"VALUES (?, ?, ?, ?, ?, ?)",
                    [(run_id, int(eid), cong, cov.get(eid), qual.get(eid), fetched_at)
                     for eid, cong in edge_congestion.items()],
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
        ).add_to(m)
        return m

    def plot_comparison(
        self,
        mode:    str       = "driving",
        zoom:    int       = 13,
        sources: list[str] = None,
    ) -> None:
        """Side-by-side folium maps for each source. Renders in Jupyter notebooks."""
        from html import escape
        from IPython.display import HTML, display

        if sources is None:
            sources = ["mapbox", "google"]

        gdf = self.get_congestion_comparison(mode=mode, sources=sources)
        viz = gdf[gdf.geometry.geom_type.isin(["LineString", "MultiLineString"])].copy()
        if viz.empty:
            print("No edges to plot.")
            return

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
            ).add_to(m)
            return escape(m.get_root().render())

        panels = "".join(
            f'<div style="flex:1"><h4 style="text-align:center;margin:4px 0">{s.title()}</h4>'
            f'<iframe srcdoc="{_make_map(f"congestion_{s}")}"'
            f' width="100%" height="460" frameborder="0"></iframe></div>'
            for s in sources
            if f"congestion_{s}" in viz.columns
        )
        display(HTML(f'<div style="display:flex;gap:8px">{panels}</div>'))
