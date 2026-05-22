"""
Mapbox traffic tile fetching and map-matching to OSM edges.

Usage:
    from scripts.mapbox_traffic import MapboxTraffic

    traffic = MapboxTraffic(token=MAPBOX_TOKEN, zoom=14)
    gdf = traffic.fetch(boundary_path, tiles_dir, out_file)
    edge_cong = traffic.map_match(db_path, gdf)
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

_TRAFFIC_URL = "https://api.mapbox.com/v4/mapbox.mapbox-traffic-v1/{z}/{x}/{y}.mvt"
_STREETS_URL = "https://api.mapbox.com/v4/mapbox.mapbox-streets-v8/{z}/{x}/{y}.mvt"
_SEVERITY    = {"severe": 4, "heavy": 3, "moderate": 2, "low": 1}


# ── Private helpers ───────────────────────────────────────────────────────────

def _download_tile(url_tpl: str, tile, out_dir: Path, token: str, retries: int = 3):
    path = out_dir / f"{tile.z}_{tile.x}_{tile.y}.mvt"
    if path.exists():
        return tile, path
    url = url_tpl.format(z=tile.z, x=tile.x, y=tile.y)
    for attempt in range(retries):
        try:
            r = requests.get(url, params={"access_token": token}, timeout=(5, 20))
        except requests.exceptions.Timeout:
            time.sleep(2 ** attempt)
            continue
        except requests.exceptions.RequestException:
            return tile, None
        if r.status_code == 200:
            path.write_bytes(r.content)
            return tile, path
        if r.status_code == 429:
            time.sleep(2 ** attempt)
        else:
            return tile, None
    return tile, None


def _decode_mvt(path: Path, tile, layer_name: str) -> list:
    data    = path.read_bytes()
    decoded = mapbox_vector_tile.decode(data, default_options={"y_coord_down": True})
    layer   = decoded.get(layer_name, {})
    extent  = layer.get("extent", 4096)
    b       = mercantile.bounds(tile)
    dx, dy  = (b.east - b.west) / extent, (b.north - b.south) / extent
    matrix  = [dx, 0, 0, -dy, b.west, b.north]
    features = []
    for feat in layer.get("features", []):
        geom = affine_transform(shape(feat["geometry"]), matrix)
        features.append({
            "type": "Feature",
            "geometry": mapping(geom),
            "properties": {**feat["properties"], "tile": f"{tile.z}/{tile.x}/{tile.y}"},
        })
    return features


def _bearing(geom) -> float:
    c = get_coordinates(geom)
    return float(np.degrees(np.arctan2(c[-1][0] - c[0][0], c[-1][1] - c[0][1])) % 360)


def _dir_diff(b1: float, b2: float) -> float:
    d = abs(b1 - b2) % 360
    return min(d, 180 - d)


def _all_matches(t_geom, osm_gdf, sindex,
                 buf_m: float = 25, dir_thresh: float = 45,
                 overlap_thresh: float = 0.40) -> list[int]:
    corridor = t_geom.buffer(buf_m)
    hits     = list(sindex.intersection(corridor.bounds))
    t_bear   = _bearing(t_geom)
    return [
        i for i in hits
        if _dir_diff(t_bear, _bearing(osm_gdf.iloc[i].geometry)) <= dir_thresh
        and corridor.intersection(osm_gdf.iloc[i].geometry).length
            / max(osm_gdf.iloc[i].geometry.length, 1e-6) >= overlap_thresh
    ]


# ── Public class ──────────────────────────────────────────────────────────────

class MapboxTraffic:
    """
    Fetch Mapbox traffic tiles and map-match congestion to OSM edges.

    Parameters
    ----------
    token   : Mapbox access token (pk.eyJ1...)
    zoom    : tile zoom level (default 14)
    workers : parallel download threads (default 6)
    """

    def __init__(self, token: str, zoom: int = 14, workers: int = 6) -> None:
        self.token   = token
        self.zoom    = zoom
        self.workers = workers

    def fetch(self, boundary_path: str | Path, tiles_dir: str | Path,
              out_file: str | Path) -> gpd.GeoDataFrame:
        """
        Download Mapbox traffic + streets MVT tiles, spatially join them, clip to
        boundary, and save as GeoJSON.

        If out_file already exists it is loaded from disk (tile fetch is skipped).

        Returns the GeoDataFrame of street segments with a 'congestion' column.
        """
        boundary_path = Path(boundary_path)
        tiles_dir     = Path(tiles_dir)
        out_file      = Path(out_file)

        if out_file.exists():
            return gpd.read_file(out_file)

        gdf      = gpd.read_file(boundary_path).to_crs("EPSG:4326")
        boundary = gdf.geometry.union_all()
        w, s, e, n = boundary.bounds
        tiles = [
            t for t in mercantile.tiles(w, s, e, n, zooms=self.zoom)
            if boundary.intersects(box(*mercantile.bounds(t)))
        ]

        (tiles_dir / "traffic").mkdir(parents=True, exist_ok=True)
        (tiles_dir / "streets").mkdir(parents=True, exist_ok=True)

        traffic_paths: dict = {}
        streets_paths: dict = {}

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futs = {
                pool.submit(_download_tile, _TRAFFIC_URL, t, tiles_dir / "traffic", self.token): ("t", t)
                for t in tiles
            }
            futs.update({
                pool.submit(_download_tile, _STREETS_URL, t, tiles_dir / "streets", self.token): ("s", t)
                for t in tiles
            })
            for f in as_completed(futs):
                kind, tile = futs[f]
                _, path = f.result()
                if path:
                    (traffic_paths if kind == "t" else streets_paths)[tile] = path

        traffic_feats: list = []
        streets_feats: list = []
        for tile, path in traffic_paths.items():
            traffic_feats.extend(_decode_mvt(path, tile, "traffic"))
        for tile, path in streets_paths.items():
            streets_feats.extend(_decode_mvt(path, tile, "road"))

        if not streets_feats:
            raise RuntimeError("No streets features decoded — check that MAPBOX_TOKEN is valid.")

        streets_gdf = (
            gpd.GeoDataFrame.from_features(streets_feats, crs="EPSG:4326")
            .explode(index_parts=False).reset_index(drop=True)
        )
        if traffic_feats:
            traffic_gdf = (
                gpd.GeoDataFrame.from_features(traffic_feats, crs="EPSG:4326")
                .explode(index_parts=False).reset_index(drop=True)
            )
        else:
            traffic_gdf = gpd.GeoDataFrame(columns=["congestion", "geometry"])

        streets_m = streets_gdf.to_crs("EPSG:3857")
        traffic_m = traffic_gdf[["congestion", "geometry"]].to_crs("EPSG:3857") \
            if len(traffic_gdf) else traffic_gdf

        joined = gpd.sjoin_nearest(
            streets_m, traffic_m, how="left", max_distance=20, distance_col="_d"
        ).drop(columns=["index_right", "_d"], errors="ignore")
        joined = joined[~joined.index.duplicated(keep="first")].to_crs("EPSG:4326")
        joined["congestion"] = joined["congestion"].fillna("no data")
        clipped = gpd.clip(joined, gdf)

        out_file.parent.mkdir(parents=True, exist_ok=True)
        clipped.to_file(out_file, driver="GeoJSON")
        return clipped

    def map_match(self, db_path: str | Path,
                  traffic_gdf: gpd.GeoDataFrame) -> dict[int, str]:
        """
        Match Mapbox street segments (with congestion) to OSM driving edges.

        Uses a 25 m corridor buffer, bearing filter (≤ 45°), and 40% overlap
        threshold. The most severe congestion wins when multiple segments match
        the same edge.

        Parameters
        ----------
        db_path     : path to the DuckDB file
        traffic_gdf : GeoDataFrame returned by fetch() with 'congestion' column

        Returns
        -------
        dict mapping edge_id (int) → congestion level string
        """
        import duckdb
        con = duckdb.connect(str(db_path), read_only=True)
        con.execute("LOAD spatial")
        df = con.execute(
            "SELECT edge_id, ST_AsText(geometry) wkt_geom FROM driving.edges"
        ).df()
        con.close()

        edges = gpd.GeoDataFrame(
            df, geometry=gpd.GeoSeries.from_wkt(df["wkt_geom"]), crs="EPSG:4326"
        ).reset_index(drop=True)

        traffic = traffic_gdf[
            traffic_gdf.geometry.geom_type.isin(["LineString", "MultiLineString"])
            & (traffic_gdf["congestion"] != "no data")
        ].reset_index(drop=True)

        edges_m   = edges.to_crs("EPSG:3857").reset_index(drop=True)
        traffic_m = traffic.to_crs("EPSG:3857").reset_index(drop=True)
        sindex    = edges_m.sindex

        edge_cong: dict[int, str] = {}
        for _, row in traffic_m.iterrows():
            matched = _all_matches(row.geometry, edges_m, sindex)
            cong    = row["congestion"]
            for idx in matched:
                eid = int(edges_m.iloc[idx]["edge_id"])
                if _SEVERITY.get(cong, 0) > _SEVERITY.get(edge_cong.get(eid, ""), 0):
                    edge_cong[eid] = cong
        return edge_cong
