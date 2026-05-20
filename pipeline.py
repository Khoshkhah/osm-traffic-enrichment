"""
osm-traffic-enrichment — end-to-end CLI pipeline

Usage (PBF already downloaded):
    python pipeline.py \\
        --pbf         /path/to/region.osm.pbf \\
        --boundary    boundaries/tartu.geojson \\
        --name        tartu \\
        --zoom        14

Usage (auto-download PBF from Geofabrik):
    python pipeline.py \\
        --country-url https://download.geofabrik.de/europe/estonia-latest.osm.pbf \\
        --boundary    boundaries/tartu.geojson \\
        --name        tartu \\
        --zoom        14

Steps run automatically:
    0. Download country PBF     (if --country-url given and not yet cached)
    1. Filter PBF by boundary   (osmium extract)
    2. Build road network       (duckOSM)
    3. Fetch Mapbox tiles       (traffic-v1 + streets-v8)
    4. Map match + enrich       (writes congestion to DuckDB)

Output:
    db/{name}.duckdb              — enriched DuckDB (driving.edges has congestion column)
    output/{name}_edges_traffic.geojson
    output/{name}_edges_traffic.csv
"""

import argparse
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import duckdb
import geopandas as gpd
import mapbox_vector_tile
import mercantile
import numpy as np
import requests
from dotenv import load_dotenv
from shapely import get_coordinates
from shapely.affinity import affine_transform
from shapely.geometry import box, mapping, shape

load_dotenv(Path(__file__).parent / ".env", override=True)

BASE_DIR = Path(__file__).parent
TRAFFIC_URL = "https://api.mapbox.com/v4/mapbox.mapbox-traffic-v1/{z}/{x}/{y}.mvt"
STREETS_URL = "https://api.mapbox.com/v4/mapbox.mapbox-streets-v8/{z}/{x}/{y}.mvt"
SEVERITY = {"severe": 4, "heavy": 3, "moderate": 2, "low": 1}


# ── Step 0 — Download country PBF ───────────────────────────────────────

def download_pbf(url: str, map_dir: Path) -> Path:
    """Download a country PBF from Geofabrik if not already cached."""
    out_path = map_dir / Path(url).name
    if out_path.exists():
        size_mb = out_path.stat().st_size / 1_048_576
        print(f"  Cached: {out_path.name}  ({size_mb:.0f} MB) — skipping download")
        return out_path
    print(f"  Downloading {url} ...")
    with requests.get(url, stream=True, timeout=30) as r:
        r.raise_for_status()
        total      = int(r.headers.get("content-length", 0))
        downloaded = 0
        with open(out_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8 * 1024 * 1024):
                f.write(chunk)
                downloaded += len(chunk)
                if total:
                    print(f"  {downloaded/1_048_576:.0f} / {total/1_048_576:.0f} MB"
                          f"  ({downloaded/total*100:.0f}%)", end="\r")
    size_mb = out_path.stat().st_size / 1_048_576
    print(f"\n  Done: {out_path.name}  ({size_mb:.0f} MB)")
    return out_path


# ── Step 1 — Filter PBF ──────────────────────────────────────────────────

def filter_pbf(pbf_input: Path, boundary: Path, output: Path) -> None:
    print(f"\n[1/4] Filtering PBF by boundary...")
    if output.exists():
        print(f"  Cached: {output.name} — skipping osmium extract")
        return
    cmd = ["osmium", "extract", "--polygon", str(boundary.resolve()),
           "--output", str(output.resolve()), "--overwrite", str(pbf_input.resolve())]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit(f"osmium extract failed:\n{result.stderr}")
    print(f"  Done: {output.name}  ({output.stat().st_size / 1_048_576:.1f} MB)")


# ── Step 2 — Build network ───────────────────────────────────────────────

def build_network(pbf: Path, boundary: Path, name: str, db_path: Path,
                  config_path: Path | None) -> None:
    print(f"\n[2/4] Building road network with duckOSM...")
    if db_path.exists():
        print(f"  Cached: {db_path.name} — skipping duckOSM")
        return
    try:
        from duckosm import DuckOSM, Config
    except ImportError:
        sys.exit("duckOSM not found. Install: pip install -e ../h3-routing-platform/tools/duckOSM")

    if config_path and config_path.exists():
        config = Config.from_yaml(str(config_path))
    else:
        config = Config.from_args(
            pbf_path=str(pbf), output_path=str(db_path.parent),
            name=name, boundary_path=str(boundary), modes=["driving"],
            build_graph=True, h3_indexing=True, h3_resolution=8,
            simplify=True, process_speeds=True,
            extract_restrictions=True, calculate_costs=True,
        )
    result_path = DuckOSM(config).run()
    size = result_path.stat().st_size / 1_048_576
    print(f"  Done: {result_path.name}  ({size:.1f} MB)")


# ── Step 3 — Fetch Mapbox tiles ──────────────────────────────────────────

def _download_tile(url_tpl, tile, out_dir, token, retries=3):
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


def _decode_mvt(path, tile, layer_name):
    data = path.read_bytes()
    decoded = mapbox_vector_tile.decode(data, default_options={"y_coord_down": True})
    layer = decoded.get(layer_name, {})
    extent = layer.get("extent", 4096)
    b = mercantile.bounds(tile)
    dx = (b.east - b.west) / extent
    dy = (b.north - b.south) / extent
    matrix = [dx, 0, 0, -dy, b.west, b.north]
    features = []
    for feat in layer.get("features", []):
        geom = affine_transform(shape(feat["geometry"]), matrix)
        features.append({
            "type": "Feature",
            "geometry": mapping(geom),
            "properties": {**feat["properties"], "tile": f"{tile.z}/{tile.x}/{tile.y}"},
        })
    return features


def fetch_traffic(boundary_path: Path, zoom: int, token: str,
                  tiles_dir: Path, out_file: Path) -> None:
    print(f"\n[3/4] Fetching Mapbox traffic tiles (zoom {zoom})...")
    if out_file.exists():
        print(f"  Cached: {out_file.name} — skipping download")
        return

    gdf = gpd.read_file(boundary_path).to_crs("EPSG:4326")
    boundary = gdf.geometry.union_all()
    w, s, e, n = boundary.bounds
    tiles = [t for t in mercantile.tiles(w, s, e, n, zooms=zoom)
             if boundary.intersects(box(*mercantile.bounds(t)))]
    print(f"  {len(tiles)} tiles to fetch")

    (tiles_dir / "traffic").mkdir(parents=True, exist_ok=True)
    (tiles_dir / "streets").mkdir(parents=True, exist_ok=True)

    traffic_paths, streets_paths = {}, {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = {pool.submit(_download_tile, TRAFFIC_URL, t, tiles_dir/"traffic", token): ("t", t)
                for t in tiles}
        futs.update({pool.submit(_download_tile, STREETS_URL, t, tiles_dir/"streets", token): ("s", t)
                     for t in tiles})
        for f in as_completed(futs):
            kind, tile = futs[f]
            _, path = f.result()
            if path:
                (traffic_paths if kind == "t" else streets_paths)[tile] = path

    traffic_feats, streets_feats = [], []
    for tile, path in traffic_paths.items():
        traffic_feats.extend(_decode_mvt(path, tile, "traffic"))
    for tile, path in streets_paths.items():
        streets_feats.extend(_decode_mvt(path, tile, "road"))

    streets_gdf = (gpd.GeoDataFrame.from_features(streets_feats, crs="EPSG:4326")
                   .explode(index_parts=False).reset_index(drop=True))
    traffic_gdf = (gpd.GeoDataFrame.from_features(traffic_feats, crs="EPSG:4326")
                   .explode(index_parts=False).reset_index(drop=True))

    streets_m = streets_gdf.to_crs("EPSG:3857")
    traffic_m = traffic_gdf[["congestion", "geometry"]].to_crs("EPSG:3857")
    joined = gpd.sjoin_nearest(streets_m, traffic_m, how="left",
                               max_distance=20, distance_col="_d"
                               ).drop(columns=["index_right", "_d"], errors="ignore")
    joined = joined[~joined.index.duplicated(keep="first")].to_crs("EPSG:4326")
    joined["congestion"] = joined["congestion"].fillna("no data")
    clipped = gpd.clip(joined, gdf)
    clipped.to_file(out_file, driver="GeoJSON")
    print(f"  Done: {out_file.name}  ({len(clipped):,} segments)")


# ── Step 4 — Map match + write to DuckDB ────────────────────────────────

def _bearing(geom):
    c = get_coordinates(geom)
    return np.degrees(np.arctan2(c[-1][0] - c[0][0], c[-1][1] - c[0][1])) % 360


def _dir_diff(b1, b2):
    d = abs(b1 - b2) % 360
    return min(d, 180 - d)


def _all_matches(t_geom, osm_gdf, sindex, buf=25, dir_thresh=45, overlap_thresh=0.40):
    corridor = t_geom.buffer(buf)
    hits = list(sindex.intersection(corridor.bounds))
    t_bear = _bearing(t_geom)
    return [i for i in hits
            if _dir_diff(t_bear, _bearing(osm_gdf.iloc[i].geometry)) <= dir_thresh
            and corridor.intersection(osm_gdf.iloc[i].geometry).length
                / max(osm_gdf.iloc[i].geometry.length, 1e-6) >= overlap_thresh]


def map_match(db_path: Path, traffic_file: Path, name: str, output_dir: Path) -> None:
    print(f"\n[4/4] Map matching traffic → OSM edges...")

    con = duckdb.connect(str(db_path))
    con.execute("LOAD spatial")
    df = con.execute(
        "SELECT edge_id, highway, name, oneway, length_m, ST_AsText(geometry) wkt_geom "
        "FROM driving.edges"
    ).df()
    edges = gpd.GeoDataFrame(df, geometry=gpd.GeoSeries.from_wkt(df["wkt_geom"]),
                             crs="EPSG:4326").reset_index(drop=True)

    traffic = gpd.read_file(traffic_file)
    traffic = traffic[
        traffic.geometry.geom_type.isin(["LineString", "MultiLineString"])
        & (traffic["congestion"] != "no data")
    ].reset_index(drop=True)

    edges_m   = edges.to_crs("EPSG:3857").reset_index(drop=True)
    traffic_m = traffic.to_crs("EPSG:3857").reset_index(drop=True)
    sindex    = edges_m.sindex

    edge_cong, total_pairs = {}, 0
    for _, row in traffic_m.iterrows():
        matched = _all_matches(row.geometry, edges_m, sindex)
        cong = row["congestion"]
        for idx in matched:
            eid = int(edges_m.iloc[idx]["edge_id"])
            if SEVERITY.get(cong, 0) > SEVERITY.get(edge_cong.get(eid, ""), 0):
                edge_cong[eid] = cong
        total_pairs += len(matched)

    con.execute("ALTER TABLE driving.edges ADD COLUMN IF NOT EXISTS congestion VARCHAR DEFAULT 'no data'")
    con.executemany("UPDATE driving.edges SET congestion = ? WHERE edge_id = ?",
                    [(c, e) for e, c in edge_cong.items()])
    con.close()

    matched_pct = len(edge_cong) / max(len(edges), 1) * 100
    print(f"  Edges matched : {len(edge_cong):,} / {len(edges):,}  ({matched_pct:.1f}%)")
    print(f"  Total pairs   : {total_pairs:,}  (avg {total_pairs/max(len(traffic_m),1):.1f} per segment)")
    print(f"  Written to    : {db_path}")

    edges["congestion"] = edges["edge_id"].map(edge_cong).fillna("no data")
    edges.to_file(output_dir / f"{name}_edges_traffic.geojson", driver="GeoJSON")
    edges.drop(columns=["geometry", "wkt_geom"], errors="ignore").to_csv(
        output_dir / f"{name}_edges_traffic.csv", index=False)
    print(f"  Exports saved to {output_dir}/")


# ── Main ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="OSM traffic enrichment pipeline")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--pbf",         help="Local OSM PBF file (already downloaded)")
    group.add_argument("--country-url", help="Geofabrik URL to download country PBF automatically")
    parser.add_argument("--boundary", required=True, help="Boundary GeoJSON file")
    parser.add_argument("--name",     required=True, help="Area name (used for output filenames)")
    parser.add_argument("--zoom",     type=int, default=14, help="Mapbox tile zoom level (default 14)")
    parser.add_argument("--config",   default=None, help="duckOSM YAML config (optional)")
    args = parser.parse_args()

    token = os.environ.get("MAPBOX_ACCESS_TOKEN", "")
    if not token or not token.startswith("pk."):
        sys.exit("ERROR: set MAPBOX_ACCESS_TOKEN in .env (must start with pk.)")

    boundary     = Path(args.boundary).resolve()
    config_path  = Path(args.config).resolve() if args.config else \
                   BASE_DIR / "config" / f"{args.name}.yaml"

    map_dir    = BASE_DIR / "map";    map_dir.mkdir(exist_ok=True)
    pbf_dir    = BASE_DIR / "pbf";    pbf_dir.mkdir(exist_ok=True)
    db_dir     = BASE_DIR / "db";     db_dir.mkdir(exist_ok=True)
    tiles_dir  = BASE_DIR / "tiles";  tiles_dir.mkdir(exist_ok=True)
    output_dir = BASE_DIR / "output"; output_dir.mkdir(exist_ok=True)

    filtered_pbf = pbf_dir    / f"{args.name}.osm.pbf"
    db_path      = db_dir     / f"{args.name}.duckdb"
    traffic_file = output_dir / f"{args.name}_traffic.geojson"

    t0 = time.time()

    # Step 0 — download country PBF if URL provided
    if args.country_url:
        print(f"\n[0/4] Downloading country PBF...")
        pbf_input = download_pbf(args.country_url, map_dir)
    else:
        pbf_input = Path(args.pbf).resolve()
        if not pbf_input.exists():
            sys.exit(f"ERROR: PBF file not found: {pbf_input}")

    filter_pbf(pbf_input, boundary, filtered_pbf)
    build_network(filtered_pbf, boundary, args.name, db_path, config_path)
    fetch_traffic(boundary, args.zoom, token, tiles_dir, traffic_file)
    map_match(db_path, traffic_file, args.name, output_dir)

    elapsed = time.time() - t0
    print(f"\n✓ Pipeline complete in {elapsed:.0f}s")
    print(f"  DuckDB  → {db_path}")
    print(f"  GeoJSON → {output_dir}/{args.name}_edges_traffic.geojson")
    print(f"  CSV     → {output_dir}/{args.name}_edges_traffic.csv")


if __name__ == "__main__":
    main()
