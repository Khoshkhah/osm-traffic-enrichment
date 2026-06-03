"""
TomTom Traffic Flow tile fetching and map-matching to OSM edges.

TomTom tiles use a relative traffic_level float (0.0 = blocked, 1.0 = free flow).
The raw traffic_level is stored alongside the classified congestion label in
tomtom_congestion_history so that thresholds can be re-applied later without re-fetching.

Usage:
    from scripts.tomtom_traffic import TomTomTraffic

    traffic = TomTomTraffic(api_key=TOMTOM_API_KEY, zoom=14)
    gdf = traffic.fetch(boundary_path, tiles_dir, out_file)
    edge_cong, traffic_levels = traffic.map_match(db_path, gdf)
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import geopandas as gpd
import mercantile
import numpy as np
import requests
import mapbox_vector_tile
from shapely import get_coordinates
from shapely.affinity import affine_transform
from shapely.geometry import box, mapping, shape

_TILE_URL = (
    "https://api.tomtom.com/traffic/map/4/tile/flow/relative/{z}/{x}/{y}.pbf"
)
_LAYER_NAME = "Traffic flow"
_SEVERITY   = {"severe": 4, "heavy": 3, "moderate": 2, "low": 1}

# traffic_level thresholds: if level < threshold → classified as that level
_LEVEL_THRESHOLDS = [
    ("severe",   0.40),
    ("heavy",    0.60),
    ("moderate", 0.85),
]


# ── Private helpers ───────────────────────────────────────────────────────────

def _traffic_level_to_congestion(level, road_closure: bool = False) -> str:
    if road_closure:
        return "severe"
    if level is None:
        return "no data"
    for lvl, threshold in _LEVEL_THRESHOLDS:
        if level < threshold:
            return lvl
    return "low"


def _download_tomtom_tile(tile, out_dir: Path, api_key: str, retries: int = 3):
    path = out_dir / f"{tile.z}_{tile.x}_{tile.y}.pbf"
    if path.exists() and path.stat().st_size > 0:
        return tile, path
    url = _TILE_URL.format(z=tile.z, x=tile.x, y=tile.y)
    for attempt in range(retries):
        try:
            r = requests.get(url, params={"key": api_key}, timeout=(5, 20))
        except requests.exceptions.Timeout:
            time.sleep(2 ** attempt)
            continue
        if r.status_code == 200:
            path.write_bytes(r.content)
            return tile, path
        if r.status_code == 429:
            time.sleep(2 ** attempt)
        else:
            return tile, None
    return tile, None


def _decode_tomtom_tile(path: Path, tile) -> list[dict]:
    """Decode one TomTom PBF tile → list of row dicts in EPSG:4326."""
    data    = mapbox_vector_tile.decode(
        path.read_bytes(), default_options={"y_coord_down": True}
    )
    layer   = data.get(_LAYER_NAME, {})
    features = layer.get("features", [])
    if not features:
        return []

    bounds = mercantile.bounds(tile)
    extent = layer.get("extent", 4096)
    dx     = (bounds.east  - bounds.west)  / extent
    dy     = (bounds.north - bounds.south) / extent
    matrix = [dx, 0, 0, -dy, bounds.west, bounds.north]

    rows = []
    for feat in features:
        props   = feat.get("properties", {})
        level   = props.get("traffic_level")
        closure = props.get("road_closure", False)
        rows.append({
            "geometry":      affine_transform(shape(feat["geometry"]), matrix),
            "congestion":    _traffic_level_to_congestion(level, closure),
            "traffic_level": round(level, 4) if level is not None else None,
            "road_closure":  closure,
            "road_type":     props.get("road_type", ""),
        })
    return rows


def _bearing(geom) -> float:
    c = get_coordinates(geom)
    return float(np.degrees(np.arctan2(c[-1][1] - c[0][1], c[-1][0] - c[0][0])) % 360)


def _dir_diff(b1: float, b2: float) -> float:
    d = abs(b1 - b2) % 360
    return min(d, 180 - d)


# ── Public class ──────────────────────────────────────────────────────────────

class TomTomTraffic:
    """
    Fetch TomTom Traffic Flow tiles and map-match congestion to OSM edges.

    The raw traffic_level float (0.0 = blocked, 1.0 = free flow) is preserved
    in the returned traffic_levels dict so it can be stored in the DB for
    re-classification without re-fetching.

    Parameters
    ----------
    api_key : TomTom API key
    zoom    : tile zoom level (default 14)
    workers : parallel download threads (default 6)
    """

    BUF_M          = 25    # metres corridor
    DIR_THRESH     = 45    # degrees bearing tolerance
    OVERLAP_THRESH = 0.40  # minimum edge-in-corridor overlap fraction

    def __init__(self, api_key: str, zoom: int = 14, workers: int = 6,
                 matcher: str = "route", match_cfg: dict | None = None) -> None:
        self.api_key   = api_key
        self.zoom      = zoom
        self.workers   = workers
        self.n_tiles   = 0  # set by .fetch()
        self.matcher   = matcher          # "route" (network_matching) or "geometric" (legacy)
        self.match_cfg = match_cfg or {}
        self.last_match_report: dict | None = None  # match outcome from the route matcher
        self.last_edge_extra:   dict | None = None  # edge_id → {covering_match, quality_match, …}

    def fetch(self, boundary_path: str | Path, tiles_dir: str | Path,
              out_file: str | Path) -> gpd.GeoDataFrame:
        """
        Download TomTom traffic PBF tiles, decode, classify, clip to boundary,
        and save as GeoJSON.

        If out_file already exists it is loaded from disk.

        Returns GeoDataFrame with columns:
            geometry, congestion, traffic_level, road_closure, road_type
        """
        boundary_path = Path(boundary_path)
        tiles_dir     = Path(tiles_dir)
        out_file      = Path(out_file)

        gdf      = gpd.read_file(boundary_path).to_crs("EPSG:4326")
        boundary = gdf.geometry.union_all()
        w, s, e, n = boundary.bounds
        tiles = [
            t for t in mercantile.tiles(w, s, e, n, zooms=self.zoom)
            if boundary.intersects(box(*mercantile.bounds(t)))
        ]
        self.n_tiles = len(tiles)

        if out_file.exists():
            return gpd.read_file(out_file)

        tiles_dir.mkdir(parents=True, exist_ok=True)
        tile_paths: dict = {}

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futs = {pool.submit(_download_tomtom_tile, t, tiles_dir, self.api_key): t for t in tiles}
            for f in as_completed(futs):
                tile, path = f.result()
                if path:
                    tile_paths[tile] = path

        all_rows = []
        for tile, path in tile_paths.items():
            try:
                all_rows.extend(_decode_tomtom_tile(path, tile))
            except Exception:
                pass

        if not all_rows:
            raise RuntimeError(
                f'No features decoded — layer "{_LAYER_NAME}" not found in tiles. '
                "Check your TOMTOM_API_KEY and zoom level."
            )

        traffic = gpd.GeoDataFrame(all_rows, crs="EPSG:4326").clip(boundary)
        out_file.parent.mkdir(parents=True, exist_ok=True)
        traffic.to_file(out_file, driver="GeoJSON")
        return traffic

    @staticmethod
    def _load_edges(db_path: str | Path) -> gpd.GeoDataFrame:
        """Load driving.edges (edge_id + geometry) as a GeoDataFrame in EPSG:4326."""
        import duckdb
        con = duckdb.connect(str(db_path), read_only=True)
        con.execute("INSTALL spatial")
        con.execute("LOAD spatial")
        df = con.execute(
            "SELECT edge_id, ST_AsText(geometry) wkt_geom FROM driving.edges"
        ).df()
        con.close()
        return gpd.GeoDataFrame(
            df.drop(columns=["wkt_geom"]),
            geometry=gpd.GeoSeries.from_wkt(df["wkt_geom"]), crs="EPSG:4326",
        )

    def map_match(self, db_path: str | Path,
                  traffic_gdf: gpd.GeoDataFrame,
                  ) -> tuple[dict[int, str], dict[int, float | None]]:
        """
        Match TomTom traffic segments to OSM driving edges.

        Default matcher ("route") uses network_matching's route-based graph-DTW
        (provider → OSM, longest-overlap wins, coverage gate); the winning segment's
        raw traffic_level is carried through. Set matcher="geometric" for the legacy
        25 m corridor + bearing + overlap most-severe matcher.

        Returns
        -------
        (edge_congestion, traffic_levels)
            edge_congestion : dict edge_id → congestion level string
            traffic_levels  : dict edge_id → raw traffic_level float (or None)
        """
        if self.matcher == "geometric":
            return self._map_match_geometric(db_path, traffic_gdf)

        from scripts.route_match import route_match_segments
        edges = self._load_edges(db_path)
        edge_level, edge_extra, self.last_match_report = route_match_segments(
            edges, traffic_gdf, level_col="congestion",
            extra_cols=("traffic_level",), **self.match_cfg
        )
        self.last_edge_extra = edge_extra
        traffic_levels = {eid: d.get("traffic_level") for eid, d in edge_extra.items()}
        return edge_level, traffic_levels

    def _map_match_geometric(self, db_path: str | Path,
                             traffic_gdf: gpd.GeoDataFrame,
                             ) -> tuple[dict[int, str], dict[int, float | None]]:
        """Legacy geometric matcher: 25 m corridor, ≤ 45° bearing, ≥ 40% overlap,
        most severe wins; winning segment's traffic_level is kept."""
        import duckdb

        con = duckdb.connect(str(db_path), read_only=True)
        con.execute("INSTALL spatial")
        con.execute("LOAD spatial")
        df = con.execute(
            "SELECT edge_id, ST_AsText(geometry) wkt_geom FROM driving.edges"
        ).df()
        con.close()

        edges = gpd.GeoDataFrame(
            df, geometry=gpd.GeoSeries.from_wkt(df["wkt_geom"]), crs="EPSG:4326"
        ).reset_index(drop=True)
        edges = edges[edges.geometry.geom_type.isin(["LineString", "MultiLineString"])]

        traffic = traffic_gdf[
            traffic_gdf.geometry.geom_type.isin(["LineString", "MultiLineString"])
            & (traffic_gdf["congestion"] != "no data")
        ].reset_index(drop=True)

        edges_m   = edges.to_crs("EPSG:3857").reset_index(drop=True)
        traffic_m = traffic.to_crs("EPSG:3857").reset_index(drop=True)
        sindex    = edges_m.sindex

        edge_cong:   dict[int, str]          = {}
        edge_levels: dict[int, float | None] = {}

        for _, seg in traffic_m.iterrows():
            cong     = seg["congestion"]
            raw_lvl  = seg.get("traffic_level") if hasattr(seg, "get") else \
                       (seg["traffic_level"] if "traffic_level" in seg.index else None)
            seg_geom = seg.geometry
            seg_buf  = seg_geom.buffer(self.BUF_M)
            seg_bear = _bearing(seg_geom)

            for idx in sindex.intersection(seg_buf.bounds):
                edge    = edges_m.iloc[idx]
                edge_id = int(edge["edge_id"])
                if _dir_diff(seg_bear, _bearing(edge.geometry)) > self.DIR_THRESH:
                    continue
                overlap = edge.geometry.intersection(seg_buf).length
                if overlap / max(edge.geometry.length, 1e-6) < self.OVERLAP_THRESH:
                    continue
                if _SEVERITY.get(cong, 0) > _SEVERITY.get(edge_cong.get(edge_id, ""), 0):
                    edge_cong[edge_id]   = cong
                    edge_levels[edge_id] = raw_lvl

        return edge_cong, edge_levels
