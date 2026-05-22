"""
osm-traffic-enrichment — end-to-end CLI pipeline

SIMPLEST USAGE (everything auto-detected):
    python pipeline.py --place "Tartu, Estonia" --name tartu

COMMON USAGE PATTERNS:

  1. Place name only — boundary + PBF resolved automatically:
       python pipeline.py --place "Tartu, Estonia" --name tartu

  2. Place name + explicit PBF (already downloaded):
       python pipeline.py --place "Tartu, Estonia" --pbf map/estonia-latest.osm.pbf --name tartu

  3. Manual boundary file + auto-download PBF:
       python pipeline.py --boundary boundaries/tartu.geojson \\
                          --country-url https://download.geofabrik.de/europe/estonia-latest.osm.pbf \\
                          --name tartu

  4. Fully manual (all paths explicit):
       python pipeline.py --boundary boundaries/tartu.geojson \\
                          --pbf map/estonia-latest.osm.pbf \\
                          --name tartu --zoom 14

  5. Force full re-run from scratch:
       python pipeline.py --place "Tartu, Estonia" --name tartu --refresh

ARGUMENT SUMMARY:
    --place        Place name → auto-fetches boundary + detects country PBF URL
    --boundary     Path to an existing boundary GeoJSON  (alternative to --place)
    --name         Output name used for all files  [required]
    --pbf          Path to a local .osm.pbf file  (optional if --place or --country-url given)
    --country-url  Geofabrik download URL  (optional if --place given)
    --zoom             Mapbox tile zoom level  [default: 14]
    --google-zoom      Google Maps screenshot zoom level  [default: 16]
    --config           Path to a duckOSM YAML config (optional, auto-generated if absent)
    --refresh          Delete all area-specific cached files before running
    --traffic-source   mapbox | google | both (default)

  See docs/pipeline_cli.md for full argument documentation.
  See docs/traffic_status.md for congestion level definitions.

PIPELINE STEPS:
    [opt] Refresh cache           (--refresh)
    [opt] Fetch boundary          (--place)
    [opt] Download country PBF    (--country-url or auto-detected from --place)
    [1/4] Filter PBF by boundary  (osmium extract)
    [2/4] Build road network      (duckOSM → DuckDB)
    --traffic-source mapbox (default):
      [3/4] Fetch Mapbox tiles      (traffic-v1 + streets-v8)
      [4/4] Map match + enrich      (writes congestion_mapbox to DuckDB)
    --traffic-source google:
      [3/4] Fetch & match Google    (PNG tiles → pixel sampling → congestion_google)

OUTPUT:
    db/{name}.duckdb                        enriched DuckDB
      driving.edges.congestion_mapbox       latest Mapbox congestion
      driving.edges.congestion_google       latest Google congestion
    output/{name}_edges_traffic.geojson     road network with congestion columns
    output/{name}_edges_traffic.csv         same without geometry
    logs/pipeline_{name}_{timestamp}.log    full run log with summary
"""

import argparse
import logging
import os
import subprocess
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path


import duckdb
import geopandas as gpd
import mapbox_vector_tile
import math
import mercantile
import numpy as np
import requests
import threading
import asyncio
from collections import defaultdict
from dotenv import load_dotenv
from PIL import Image
from scipy.spatial import cKDTree
from shapely import get_coordinates
from shapely.affinity import affine_transform
from shapely.geometry import LineString
from shapely.ops import transform as shapely_transform
import pyproj
from shapely.geometry import box, mapping, shape

load_dotenv(Path(__file__).parent / ".env", override=True)

BASE_DIR = Path(__file__).parent
TRAFFIC_URL = "https://api.mapbox.com/v4/mapbox.mapbox-traffic-v1/{z}/{x}/{y}.mvt"
STREETS_URL = "https://api.mapbox.com/v4/mapbox.mapbox-streets-v8/{z}/{x}/{y}.mvt"
SEVERITY = {"severe": 4, "heavy": 3, "moderate": 2, "low": 1}

log = logging.getLogger("pipeline")


# ── Logging setup ────────────────────────────────────────────────────────────

def setup_logging(name: str, logs_dir: Path) -> Path:
    """
    Configure logging to write to both console and a timestamped log file.

    Console : INFO level  — clean progress messages
    Log file : DEBUG level — full detail including sub-step timing and counts

    Returns the path to the log file.
    """
    logs_dir.mkdir(exist_ok=True)
    ts       = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    log_path = logs_dir / f"pipeline_{name}_{ts}.log"

    fmt_file    = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s",
                                    datefmt="%Y-%m-%d %H:%M:%S")
    fmt_console = logging.Formatter("%(message)s")

    # File handler — DEBUG so nothing is missed
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt_file)

    # Console handler — INFO for clean output
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt_console)

    log.setLevel(logging.DEBUG)
    log.addHandler(fh)
    log.addHandler(ch)

    log.info(f"Log file: {log_path}")
    return log_path


# Global list that collects (label, status, elapsed_s) for every step
_step_records: list[tuple[str, str, float]] = []


class StepTimer:
    """Context manager that logs step start/end and records timing for the summary."""
    def __init__(self, label: str):
        self.label = label
        self.t0    = None

    def __enter__(self):
        self.t0 = time.time()
        log.info(f"\n{'─'*60}")
        log.info(f"  {self.label}")
        log.info(f"{'─'*60}")
        log.debug(f"Step started at {datetime.now(timezone.utc).isoformat()}")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        elapsed = time.time() - self.t0
        if exc_type:
            status = "FAILED"
            log.error(f"  FAILED after {elapsed:.1f}s — {exc_val}")
            log.debug(traceback.format_exc())
        else:
            status = "done"
            log.info(f"  ✓ Done in {elapsed:.1f}s")
        _step_records.append((self.label, status, elapsed))
        return False   # do not suppress exceptions


def write_summary(name: str, total_s: float, log_path: Path) -> None:
    """Write a timing summary table to the log at the end of the pipeline."""
    lines = [
        "",
        "=" * 62,
        f"  PIPELINE SUMMARY — {name}",
        "=" * 62,
        f"  {'Step':<36} {'Status':<10} {'Time':>6}",
        "  " + "─" * 58,
    ]
    for label, status, elapsed in _step_records:
        # Strip the [n/4] prefix for a cleaner table
        short = label.split("] ", 1)[-1] if "] " in label else label
        lines.append(f"  {short:<36} {status:<10} {elapsed:>5.1f}s")

    lines += [
        "  " + "─" * 58,
        f"  {'TOTAL':<36} {'':10} {total_s:>5.1f}s",
        "=" * 62,
        f"  Log file: {log_path}",
        "=" * 62,
    ]
    summary = "\n".join(lines)
    log.info(summary)


# ── Geofabrik country URL lookup ─────────────────────────────────────────────

# Maps ISO 3166-1 alpha-2 country codes → Geofabrik download path segments.
# URL is built as: https://download.geofabrik.de/{path}-latest.osm.pbf
GEOFABRIK = {
    # Europe
    "al": "europe/albania",          "at": "europe/austria",
    "by": "europe/belarus",          "be": "europe/belgium",
    "ba": "europe/bosnia-herzegovina","bg": "europe/bulgaria",
    "hr": "europe/croatia",          "cz": "europe/czech-republic",
    "dk": "europe/denmark",          "ee": "europe/estonia",
    "fi": "europe/finland",          "fr": "europe/france",
    "de": "europe/germany",          "gr": "europe/greece",
    "hu": "europe/hungary",          "is": "europe/iceland",
    "ie": "europe/ireland",          "it": "europe/italy",
    "lv": "europe/latvia",           "lt": "europe/lithuania",
    "lu": "europe/luxembourg",       "md": "europe/moldova",
    "me": "europe/montenegro",       "nl": "europe/netherlands",
    "mk": "europe/north-macedonia",  "no": "europe/norway",
    "pl": "europe/poland",           "pt": "europe/portugal",
    "ro": "europe/romania",          "rs": "europe/serbia",
    "sk": "europe/slovakia",         "si": "europe/slovenia",
    "es": "europe/spain",            "se": "europe/sweden",
    "ch": "europe/switzerland",      "tr": "europe/turkey",
    "ua": "europe/ukraine",          "gb": "europe/great-britain",
    "ru": "europe/russia",
    # North America
    "ca": "north-america/canada",    "mx": "north-america/mexico",
    "us": "north-america/us",
    # South America
    "ar": "south-america/argentina", "br": "south-america/brazil",
    "cl": "south-america/chile",     "co": "south-america/colombia",
    "pe": "south-america/peru",
    # Asia
    "cn": "asia/china",              "in": "asia/india",
    "id": "asia/indonesia",          "ir": "asia/iran",
    "il": "asia/israel",             "jp": "asia/japan",
    "kz": "asia/kazakhstan",         "my": "asia/malaysia",
    "np": "asia/nepal",              "pk": "asia/pakistan",
    "ph": "asia/philippines",        "sa": "asia/saudi-arabia",
    "kr": "asia/south-korea",        "lk": "asia/sri-lanka",
    "tw": "asia/taiwan",             "th": "asia/thailand",
    "ae": "asia/united-arab-emirates","vn": "asia/vietnam",
    # Africa
    "eg": "africa/egypt",            "et": "africa/ethiopia",
    "gh": "africa/ghana",            "ke": "africa/kenya",
    "ma": "africa/morocco",          "ng": "africa/nigeria",
    "za": "africa/south-africa",     "tz": "africa/tanzania",
    "ug": "africa/uganda",
    # Australia / Oceania
    "au": "australia-oceania/australia",
    "nz": "australia-oceania/new-zealand",
}


def country_url_from_code(country_code: str) -> str | None:
    """Return the Geofabrik download URL for an ISO country code, or None."""
    path = GEOFABRIK.get(country_code.lower())
    if not path:
        return None
    return f"https://download.geofabrik.de/{path}-latest.osm.pbf"


# ── Step 0a — Fetch boundary from place name ─────────────────────────────────

def fetch_boundary(place_name: str, boundaries_dir: Path, name: str
                   ) -> tuple[Path, str | None]:
    """
    Download the administrative boundary for a named place via Nominatim
    and save it as boundaries/{name}.geojson.

    Returns (boundary_path, country_code).
    country_code is the ISO 3166-1 alpha-2 code (e.g. 'ee', 'se') — used to
    auto-resolve the Geofabrik URL when --country-url is not given.

    Cached: if boundaries/{name}.geojson already exists the file is reused
    but Nominatim is still queried to get the country code.
    """
    out_path = boundaries_dir / f"{name}.geojson"

    log.info(f"  Place   : {place_name}")
    log.info(f"  Source  : nominatim.openstreetmap.org")

    resp = requests.get(
        "https://nominatim.openstreetmap.org/search",
        params={"q": place_name, "format": "json", "limit": 5,
                "polygon_geojson": 1, "addressdetails": 1},
        headers={"User-Agent": "osm-traffic-enrichment/1.0"},
        timeout=15,
    )
    resp.raise_for_status()
    results = resp.json()

    if not results:
        raise ValueError(f"Nominatim returned no results for '{place_name}'")

    # Prefer an administrative relation with a polygon
    best = next((r for r in results if r["osm_type"] == "relation"
                 and r.get("geojson", {}).get("type") in ("Polygon", "MultiPolygon")),
                results[0])

    country_code = best.get("address", {}).get("country_code")
    log.info(f"  Found   : {best['display_name'][:70]}")
    log.info(f"  OSM ID  : {best['osm_id']}  ({best.get('geojson', {}).get('type', '?')})")
    log.info(f"  Country : {country_code or 'unknown'}")

    if out_path.exists():
        log.info(f"  Cached  : {out_path.name} already exists — skipping save")
        _step_records.append(("Fetch boundary from place name", "skipped", 0.0))
        return out_path, country_code

    if not best.get("geojson"):
        raise ValueError(f"No polygon returned for '{place_name}' — try a more specific name")

    gdf = gpd.GeoDataFrame(
        [{"name": name, "osm_id": best["osm_id"], "place": place_name}],
        geometry=[shape(best["geojson"])],
        crs="EPSG:4326",
    )
    boundaries_dir.mkdir(exist_ok=True)
    gdf.to_file(out_path, driver="GeoJSON")

    bounds = gdf.total_bounds
    log.info(f"  Bounds  : W={bounds[0]:.4f} S={bounds[1]:.4f} "
             f"E={bounds[2]:.4f} N={bounds[3]:.4f}")
    log.info(f"  Saved   : {out_path}")
    return out_path, country_code


# ── Step 0b — Download country PBF ──────────────────────────────────────────

def download_pbf(url: str, map_dir: Path) -> Path:
    out_path = map_dir / Path(url).name
    if out_path.exists():
        size_mb = out_path.stat().st_size / 1_048_576
        log.info(f"  Cached: {out_path.name}  ({size_mb:.0f} MB) — skipping download")
        log.debug(f"  Cache path: {out_path}")
        _step_records.append(("Download country PBF", "skipped", 0.0))
        return out_path

    log.info(f"  Source : {url}")
    with requests.get(url, stream=True, timeout=30) as r:
        r.raise_for_status()
        total      = int(r.headers.get("content-length", 0))
        downloaded = 0
        log.info(f"  Size   : {total/1_048_576:.0f} MB")
        with open(out_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8 * 1024 * 1024):
                f.write(chunk)
                downloaded += len(chunk)
                if total:
                    pct = downloaded / total * 100
                    log.debug(f"  Progress: {downloaded/1_048_576:.0f} / {total/1_048_576:.0f} MB  ({pct:.0f}%)")
                    print(f"  {downloaded/1_048_576:.0f} / {total/1_048_576:.0f} MB  ({pct:.0f}%)", end="\r")

    size_mb = out_path.stat().st_size / 1_048_576
    print()  # newline after progress line
    log.info(f"  Saved  : {out_path.name}  ({size_mb:.0f} MB)")
    return out_path


# ── Step 1 — Filter PBF ─────────────────────────────────────────────────────

def filter_pbf(pbf_input: Path, boundary: Path, output: Path) -> None:
    if output.exists():
        size_mb = output.stat().st_size / 1_048_576
        log.info(f"  Cached: {output.name}  ({size_mb:.1f} MB) — skipping osmium extract")
        _step_records.append(("Filter PBF by boundary", "skipped", 0.0))
        return

    log.info(f"  Input   : {pbf_input.name}  ({pbf_input.stat().st_size/1_048_576:.0f} MB)")
    log.info(f"  Boundary: {boundary.name}")
    log.info(f"  Output  : {output.name}")

    cmd = ["osmium", "extract", "--polygon", str(boundary.resolve()),
           "--output", str(output.resolve()), "--overwrite", str(pbf_input.resolve())]
    log.debug(f"  Command : {' '.join(cmd)}")

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log.error(f"  osmium stderr:\n{result.stderr}")
        raise RuntimeError(f"osmium extract failed (exit {result.returncode})")

    size_mb = output.stat().st_size / 1_048_576
    log.info(f"  Result  : {output.name}  ({size_mb:.2f} MB)")
    if result.stderr:
        log.debug(f"  osmium output: {result.stderr.strip()}")


# ── Step 2 — Build network ───────────────────────────────────────────────────

def build_network(pbf: Path, boundary: Path, name: str, db_path: Path,
                  config_path: Path | None) -> None:
    if db_path.exists():
        size_mb = db_path.stat().st_size / 1_048_576
        log.info(f"  Cached: {db_path.name}  ({size_mb:.1f} MB) — skipping duckOSM")
        _step_records.append(("Build road network (duckOSM)", "skipped", 0.0))
        return

    try:
        from duckosm import DuckOSM, Config
    except ImportError:
        raise ImportError("duckOSM not found. Install: pip install -e ../duckOSM")

    if config_path and config_path.exists():
        log.info(f"  Config  : {config_path}")
        config = Config.from_yaml(str(config_path))
    else:
        log.info(f"  Config  : programmatic (driving mode, H3 resolution 8)")
        config = Config.from_args(
            pbf_path=str(pbf), output_path=str(db_path.parent),
            name=name, boundary_path=str(boundary), modes=["driving"],
            build_graph=True, h3_indexing=True, h3_resolution=8,
            simplify=True, process_speeds=True,
            extract_restrictions=True, calculate_costs=True,
        )

    log.info(f"  Running duckOSM...")
    result_path = DuckOSM(config).run()
    size_mb     = result_path.stat().st_size / 1_048_576
    log.info(f"  Result  : {result_path.name}  ({size_mb:.1f} MB)")

    # Log edge counts per mode
    con = duckdb.connect(str(result_path), read_only=True)
    for mode in ["driving", "walking", "cycling"]:
        try:
            n = con.execute(f"SELECT count(*) FROM {mode}.edges").fetchone()[0]
            log.info(f"  Edges   : {mode:8s} → {n:,}")
        except Exception:
            pass
    con.close()


# ── Step 3 — Fetch Mapbox tiles ──────────────────────────────────────────────

def _download_tile(url_tpl, tile, out_dir, token, retries=3):
    path = out_dir / f"{tile.z}_{tile.x}_{tile.y}.mvt"
    if path.exists():
        return tile, path
    url = url_tpl.format(z=tile.z, x=tile.x, y=tile.y)
    for attempt in range(retries):
        try:
            r = requests.get(url, params={"access_token": token}, timeout=(5, 20))
        except requests.exceptions.Timeout:
            log.debug(f"  Timeout on {tile} (attempt {attempt+1})")
            time.sleep(2 ** attempt)
            continue
        except requests.exceptions.RequestException as e:
            log.warning(f"  Network error on {tile}: {e}")
            return tile, None
        if r.status_code == 200:
            path.write_bytes(r.content)
            return tile, path
        if r.status_code == 429:
            log.debug(f"  Rate limited on {tile}, backing off")
            time.sleep(2 ** attempt)
        else:
            log.warning(f"  {tile} HTTP {r.status_code}")
            return tile, None
    return tile, None


def _decode_mvt(path, tile, layer_name):
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


def fetch_traffic(boundary_path: Path, zoom: int, token: str,
                  tiles_dir: Path, out_file: Path) -> None:
    if out_file.exists():
        log.info(f"  Cached: {out_file.name} — skipping tile fetch")
        _step_records.append(("Fetch Mapbox traffic tiles", "skipped", 0.0))
        return

    gdf      = gpd.read_file(boundary_path).to_crs("EPSG:4326")
    boundary = gdf.geometry.union_all()
    w, s, e, n = boundary.bounds
    tiles = [t for t in mercantile.tiles(w, s, e, n, zooms=zoom)
             if boundary.intersects(box(*mercantile.bounds(t)))]

    log.info(f"  Zoom    : {zoom}")
    log.info(f"  Tiles   : {len(tiles)} tiles × 2 tilesets = {len(tiles)*2} requests")

    (tiles_dir / "traffic").mkdir(parents=True, exist_ok=True)
    (tiles_dir / "streets").mkdir(parents=True, exist_ok=True)

    traffic_paths, streets_paths = {}, {}
    failed = []
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
                log.debug(f"  Downloaded [{kind}] {tile}  {path.stat().st_size:,} B")
            else:
                failed.append((kind, tile))
                log.warning(f"  FAILED [{kind}] {tile}")

    log.info(f"  Traffic : {len(traffic_paths)}/{len(tiles)} tiles ok")
    log.info(f"  Streets : {len(streets_paths)}/{len(tiles)} tiles ok")
    if failed:
        log.warning(f"  Failed  : {len(failed)} tiles — results may be incomplete")

    traffic_feats, streets_feats = [], []
    for tile, path in traffic_paths.items():
        feats = _decode_mvt(path, tile, "traffic")
        traffic_feats.extend(feats)
        log.debug(f"  Decoded traffic {tile}: {len(feats)} features")
    for tile, path in streets_paths.items():
        feats = _decode_mvt(path, tile, "road")
        streets_feats.extend(feats)
        log.debug(f"  Decoded streets {tile}: {len(feats)} features")

    log.info(f"  Decoded : {len(traffic_feats)} traffic segments, {len(streets_feats)} street segments")

    streets_gdf = (gpd.GeoDataFrame.from_features(streets_feats, crs="EPSG:4326")
                   .explode(index_parts=False).reset_index(drop=True))
    traffic_gdf = (gpd.GeoDataFrame.from_features(traffic_feats, crs="EPSG:4326")
                   .explode(index_parts=False).reset_index(drop=True))

    streets_m = streets_gdf.to_crs("EPSG:3857")
    traffic_m = traffic_gdf[["congestion", "geometry"]].to_crs("EPSG:3857")
    joined    = gpd.sjoin_nearest(streets_m, traffic_m, how="left",
                                  max_distance=20, distance_col="_d"
                                  ).drop(columns=["index_right", "_d"], errors="ignore")
    joined    = joined[~joined.index.duplicated(keep="first")].to_crs("EPSG:4326")
    joined["congestion"] = joined["congestion"].fillna("no data")
    clipped   = gpd.clip(joined, gdf)

    clipped.to_file(out_file, driver="GeoJSON")
    log.info(f"  Saved   : {out_file.name}  ({len(clipped):,} segments)")
    cong_dist = clipped["congestion"].value_counts().to_dict()
    log.debug(f"  Congestion distribution: {cong_dist}")


# ── Step 4 — Map match + write to DuckDB ────────────────────────────────────

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


def _write_congestion_to_db(con, db_path, edges_df, edge_cong, source, name, zoom, n_segments, matched_at):
    """Write congestion to runs + edge_congestion_history only. driving.edges is not modified."""
    # runs table
    con.execute("""
        CREATE TABLE IF NOT EXISTS runs (
            run_id INTEGER PRIMARY KEY, boundary_name VARCHAR, source VARCHAR,
            zoom INTEGER, fetched_at TIMESTAMP, n_tiles INTEGER, n_segments INTEGER)
    """)
    con.execute("ALTER TABLE runs ADD COLUMN IF NOT EXISTS source VARCHAR")
    run_id = con.execute("SELECT coalesce(max(run_id),0)+1 FROM runs").fetchone()[0]
    con.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?)",
                [run_id, name, source, zoom, matched_at, 0, n_segments])

    # history
    con.execute("""
        CREATE TABLE IF NOT EXISTS edge_congestion_history (
            run_id INTEGER, edge_id INTEGER, source VARCHAR,
            congestion VARCHAR, matched_at TIMESTAMP)
    """)
    con.execute("ALTER TABLE edge_congestion_history ADD COLUMN IF NOT EXISTS source VARCHAR")
    if edge_cong:
        con.executemany(
            "INSERT INTO edge_congestion_history VALUES (?,?,?,?,?)",
            [(run_id, int(eid), source, cong, matched_at) for eid, cong in edge_cong.items()]
        )
    log.info(f"  edge_congestion_history: {len(edge_cong):,} rows (run_id={run_id}, source={source})")


def map_match(db_path: Path, traffic_file: Path, name: str, output_dir: Path) -> None:
    matched_at = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')

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

    log.info(f"  OSM edges       : {len(edges):,}")
    log.info(f"  Traffic segments: {len(traffic):,} (with congestion)")

    edges_m   = edges.to_crs("EPSG:3857").reset_index(drop=True)
    traffic_m = traffic.to_crs("EPSG:3857").reset_index(drop=True)
    sindex    = edges_m.sindex

    edge_cong, total_pairs = {}, 0
    for _, row in traffic_m.iterrows():
        matched = _all_matches(row.geometry, edges_m, sindex)
        cong    = row["congestion"]
        for idx in matched:
            eid = int(edges_m.iloc[idx]["edge_id"])
            if SEVERITY.get(cong, 0) > SEVERITY.get(edge_cong.get(eid, ""), 0):
                edge_cong[eid] = cong
        total_pairs += len(matched)

    matched_pct = len(edge_cong) / max(len(edges), 1) * 100
    log.info(f"  Edges matched   : {len(edge_cong):,} / {len(edges):,}  ({matched_pct:.1f}%)")
    log.info(f"  Total pairs     : {total_pairs:,}  (avg {total_pairs/max(len(traffic_m),1):.1f} per segment)")

    _write_congestion_to_db(con, db_path, edges, edge_cong, "mapbox", name, 0, len(traffic), matched_at)
    con.close()

    edges["congestion_mapbox"] = edges["edge_id"].map(edge_cong).fillna("no data")
    log.debug(f"  Distribution: {edges['congestion_mapbox'].value_counts().to_dict()}")

    geojson_out = output_dir / f"{name}_edges_traffic.geojson"
    csv_out     = output_dir / f"{name}_edges_traffic.csv"
    edges.to_file(geojson_out, driver="GeoJSON")
    edges.drop(columns=["geometry", "wkt_geom"], errors="ignore").to_csv(csv_out, index=False)
    log.info(f"  GeoJSON saved   : {geojson_out.name}")
    log.info(f"  CSV saved       : {csv_out.name}")


# ── Step 3+4 (Google) — PNG tile fetch + direct pixel sampling ───────────────

def _rgb_to_lab(r, g, b):
    """Convert sRGB (0-255) to CIE LAB (D65 illuminant)."""
    def linearize(c):
        c = c / 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = linearize(r), linearize(g), linearize(b)
    X = (r * 0.4124 + g * 0.3576 + b * 0.1805) / 0.9505
    Y = (r * 0.2126 + g * 0.7152 + b * 0.0722) / 1.0000
    Z = (r * 0.0193 + g * 0.1192 + b * 0.9505) / 1.0890
    def f(t): return t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116
    return 116 * f(Y) - 16, 500 * (f(X) - f(Y)), 200 * (f(Y) - f(Z))


# Reference colors from Google Maps JS API TrafficLayer
# Source: googletraffic R package (dime-worldbank)
_TRAFFIC_REFS = [
    ("low",      _rgb_to_lab(17,  214, 143)),   # #11D68F teal-green
    ("moderate", _rgb_to_lab(255, 207,  67)),   # #FFCF43 yellow
    ("heavy",    _rgb_to_lab(242,  78,  66)),   # #F24E42 red
    ("severe",   _rgb_to_lab(169,  39,  39)),   # #A92727 dark red
]
_TRAFFIC_THRESHOLD = 25.0   # CIE76 units


def _build_traffic_raster(screenshot_img):
    """Classify every pixel in the playwright screenshot. Returns uint8 (0=none 1-4=level)."""
    arr    = np.array(screenshot_img).astype(np.float64)
    rgb, a = arr[:, :, :3], arr[:, :, 3]
    mask   = (a >= 200) & ~((rgb[:,:,0] > 230) & (rgb[:,:,1] > 230) & (rgb[:,:,2] > 230))
    c      = rgb / 255.0
    linear = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    Xr = (linear[:,:,0]*0.4124 + linear[:,:,1]*0.3576 + linear[:,:,2]*0.1805) / 0.9505
    Yr =  linear[:,:,0]*0.2126 + linear[:,:,1]*0.7152 + linear[:,:,2]*0.0722
    Zr = (linear[:,:,0]*0.0193 + linear[:,:,1]*0.1192 + linear[:,:,2]*0.9505) / 1.0890
    def f(t): return np.where(t > 0.008856, t ** (1/3), 7.787*t + 16/116)
    lab  = np.stack([116*f(Yr) - 16, 500*(f(Xr) - f(Yr)), 200*(f(Yr) - f(Zr))], axis=-1)
    refs = np.array([ref for _, ref in _TRAFFIC_REFS], dtype=np.float64)
    diff = lab[:, :, np.newaxis, :] - refs[np.newaxis, np.newaxis, :, :]
    dists = np.sqrt((diff ** 2).sum(axis=-1))
    raster  = np.zeros(rgb.shape[:2], dtype=np.uint8)
    traffic = mask & (dists.min(axis=-1) <= _TRAFFIC_THRESHOLD)
    raster[traffic] = dists.argmin(axis=-1)[traffic] + 1
    return raster


def _render_playwright_screenshot(boundary_path: Path, zoom: int, api_key: str,
                                   proj_fwd, proj_inv):
    """Render Google Maps traffic via playwright. Returns (img, x_min_m, y_max_m, pixel_scale)."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise RuntimeError("playwright not installed — run: pip install playwright && playwright install chromium")
    from io import BytesIO

    gdf      = gpd.read_file(boundary_path).to_crs("EPSG:4326")
    boundary = gdf.geometry.union_all()
    bw, bs, be, bn = boundary.bounds
    pad_x = (be - bw) * 0.03;  pad_y = (bn - bs) * 0.03
    bw -= pad_x;  be += pad_x;  bs -= pad_y;  bn += pad_y

    x_min_m, y_min_m = proj_fwd.transform(bw, bs)
    x_max_m, y_max_m = proj_fwd.transform(be, bn)
    pixel_scale = 40075016.686 / (256 * (2 ** zoom))
    img_w = math.ceil((x_max_m - x_min_m) / pixel_scale)
    img_h = math.ceil((y_max_m - y_min_m) / pixel_scale)

    # Cap screenshot to PIL's safe limit (~170 MP) by reducing zoom if needed
    MAX_PIXELS = 160_000_000
    while img_w * img_h > MAX_PIXELS and zoom > 12:
        zoom       -= 1
        pixel_scale = 40075016.686 / (256 * (2 ** zoom))
        img_w       = math.ceil((x_max_m - x_min_m) / pixel_scale)
        img_h       = math.ceil((y_max_m - y_min_m) / pixel_scale)
        log.warning(f"  Screenshot too large — reduced zoom to {zoom} ({img_w}×{img_h} px)")

    cx_lng, cx_lat = proj_inv.transform((x_min_m + x_max_m) / 2, (y_min_m + y_max_m) / 2)

    html = f"""<!DOCTYPE html>
<html><head>
  <style>html,body{{margin:0;padding:0;overflow:hidden;}}#map{{width:{img_w}px;height:{img_h}px;}}</style>
  <script src="https://maps.googleapis.com/maps/api/js?key={api_key}"></script>
</head><body><div id="map"></div>
<script>
  const map = new google.maps.Map(document.getElementById('map'), {{
    center: {{lat:{cx_lat}, lng:{cx_lng}}}, zoom: {zoom}, disableDefaultUI: true,
    styles: [
      {{elementType:'labels',   stylers:[{{visibility:'off'}}]}},
      {{elementType:'geometry', stylers:[{{visibility:'off'}}]}},
      {{featureType:'road', elementType:'geometry', stylers:[{{visibility:'on'}},{{color:'#ffffff'}}]}},
      {{featureType:'landscape', stylers:[{{color:'#ffffff'}},{{visibility:'on'}}]}}
    ]
  }});
  new google.maps.TrafficLayer().setMap(map);
  map.addListener('tilesloaded', () => {{ window._ready = true; }});
</script></body></html>"""

    async def _render(html, w, h):
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            page    = await browser.new_page(viewport={"width": w, "height": h})
            await page.set_content(html, wait_until="domcontentloaded")
            await page.wait_for_function("window._ready === true", timeout=30000)
            await page.wait_for_timeout(2000)
            data = await page.screenshot()
            await browser.close()
            return data

    _out = {}
    def _run(): _out["png"] = asyncio.run(_render(html, img_w, img_h))
    t = threading.Thread(target=_run)
    t.start()
    t.join(timeout=60)
    if "png" not in _out:
        raise RuntimeError("Playwright rendering timed out after 60 s")

    img = Image.open(BytesIO(_out["png"])).convert("RGBA")
    return img, x_min_m, y_max_m, pixel_scale


def fetch_and_match_google(db_path: Path, boundary_path: Path, zoom: int,
                           name: str, output_dir: Path) -> None:
    """Render Google Maps traffic via playwright and match to OSM edges using bearing-based approach."""
    matched_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    api_key = os.environ.get("GOOGLE_MAPS_API_KEY", "")
    if not api_key:
        log.error("  GOOGLE_MAPS_API_KEY not set in .env — skipping Google traffic")
        return

    proj_fwd = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    proj_inv = pyproj.Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)

    # Load edges (osm_id required for way-identity matching)
    con = duckdb.connect(str(db_path))
    con.execute("LOAD spatial")
    df = con.execute(
        "SELECT edge_id, osm_id, highway, name, length_m, ST_AsText(geometry) wkt_geom "
        "FROM driving.edges"
    ).df()
    con.close()
    edges = gpd.GeoDataFrame(df, geometry=gpd.GeoSeries.from_wkt(df["wkt_geom"]),
                             crs="EPSG:4326").reset_index(drop=True)
    edges = edges[edges.geometry.geom_type.isin(["LineString", "MultiLineString"])]
    log.info(f"  OSM edges loaded: {len(edges):,}")

    # Render screenshot
    log.info(f"  Rendering playwright screenshot (zoom={zoom})...")
    screenshot_img, x_min_m, y_max_m, pixel_scale = _render_playwright_screenshot(
        boundary_path, zoom, api_key, proj_fwd, proj_inv)
    img_w, img_h = screenshot_img.size
    log.info(f"  Screenshot      : {img_w}×{img_h} px  ({pixel_scale:.2f} m/px)")

    # Build traffic raster
    boundary_raster = _build_traffic_raster(screenshot_img)
    counts = {lvl: int((boundary_raster == i+1).sum()) for i, (lvl, _) in enumerate(_TRAFFIC_REFS)}
    total  = int((boundary_raster > 0).sum())
    log.info(f"  Traffic pixels  : {total:,}  {counts}")
    if total == 0:
        log.warning("  No traffic pixels found — congestion_google not updated")
        return

    # Traffic pixels → EPSG:3857
    traffic_rows, traffic_cols = np.where(boundary_raster > 0)
    traffic_vals = boundary_raster[traffic_rows, traffic_cols]
    pixel_xs  = x_min_m + traffic_cols * pixel_scale
    pixel_ys  = y_max_m - traffic_rows * pixel_scale
    pixel_pts = np.column_stack([pixel_xs, pixel_ys])

    # Bearing-based matching parameters
    MATCH_BUFFER_M    = 10
    NEIGHBOR_RADIUS_M = 25
    BEARING_THRESHOLD = 40
    MIN_NEIGHBORS     = 5

    # Edge sample points with local bearing
    edge_sample_pts      = []
    edge_sample_eids     = []
    edge_sample_oids     = []
    edge_sample_bearings = []

    for _, row in edges.iterrows():
        geom_m = shapely_transform(proj_fwd.transform, row.geometry)
        n_pts  = max(4, min(int(geom_m.length / 2), 500))
        oid    = int(row["osm_id"]) if int(row["osm_id"]) > 0 else -int(row["edge_id"])
        pts_m  = [geom_m.interpolate(d, normalized=True) for d in np.linspace(0.05, 0.95, n_pts)]
        for j, pt in enumerate(pts_m):
            prev = pts_m[max(0, j - 1)]
            nxt  = pts_m[min(len(pts_m) - 1, j + 1)]
            dx, dy  = nxt.x - prev.x, nxt.y - prev.y
            bearing = np.degrees(np.arctan2(dx, dy)) % 180
            edge_sample_pts.append((pt.x, pt.y))
            edge_sample_eids.append(int(row["edge_id"]))
            edge_sample_oids.append(oid)
            edge_sample_bearings.append(bearing)

    edge_pts_arr      = np.array(edge_sample_pts)
    edge_eids_arr     = np.array(edge_sample_eids,     dtype=np.int64)
    edge_oids_arr     = np.array(edge_sample_oids,     dtype=np.int64)
    edge_bearings_arr = np.array(edge_sample_bearings, dtype=np.float64)
    edge_tree         = cKDTree(edge_pts_arr)
    traffic_tree      = cKDTree(pixel_pts)
    log.info(f"  Edge sample pts : {len(edge_sample_pts):,}")

    nearby_edges   = edge_tree.query_ball_point(pixel_pts,   r=MATCH_BUFFER_M)
    nearby_traffic = traffic_tree.query_ball_point(pixel_pts, r=NEIGHBOR_RADIUS_M)

    INT_TO_LEVEL = {1: "low", 2: "moderate", 3: "heavy", 4: "severe"}
    edge_pixel_vals = defaultdict(list)
    n_matched = n_no_dir = n_no_bear = n_ambig = n_outside = 0

    for i in range(len(pixel_pts)):
        nb_e = nearby_edges[i]
        if not nb_e:
            n_outside += 1; continue
        nb_tr = nearby_traffic[i]
        if len(nb_tr) < MIN_NEIGHBORS:
            n_no_dir += 1; continue
        neighbor_pts   = pixel_pts[nb_tr]
        cov            = np.cov(neighbor_pts.T)
        _, eigvecs     = np.linalg.eigh(cov)
        direction      = eigvecs[:, 1]
        stripe_bearing = np.degrees(np.arctan2(direction[0], direction[1])) % 180
        nb_arr  = np.array(nb_e)
        eb      = edge_bearings_arr[nb_arr]
        diffs   = np.abs(stripe_bearing - eb) % 180
        diffs   = np.minimum(diffs, 180 - diffs)
        ok      = diffs <= BEARING_THRESHOLD
        if not ok.any():
            n_no_bear += 1; continue
        nb_f = nb_arr[ok]
        if len(np.unique(edge_oids_arr[nb_f])) > 1:
            n_ambig += 1; continue
        dists_local = np.linalg.norm(edge_pts_arr[nb_f] - pixel_pts[i], axis=1)
        eid = int(edge_eids_arr[nb_f[np.argmin(dists_local)]])
        edge_pixel_vals[eid].append(int(traffic_vals[i]))
        n_matched += 1

    edge_cong   = {eid: INT_TO_LEVEL[max(vals)] for eid, vals in edge_pixel_vals.items()}
    matched_pct = len(edge_cong) / max(len(edges), 1) * 100
    log.info(f"  Edges matched   : {len(edge_cong):,} / {len(edges):,}  ({matched_pct:.1f}%)")
    log.info(f"  matched={n_matched:,} no_dir={n_no_dir:,} no_bear={n_no_bear:,} ambig={n_ambig:,} outside={n_outside:,}")

    # Write to DuckDB
    con = duckdb.connect(str(db_path))
    con.execute("LOAD spatial")
    _write_congestion_to_db(con, db_path, edges, edge_cong, "google", name, zoom, total, matched_at)
    con.close()

    # Export
    edges["congestion_google"] = edges["edge_id"].map(edge_cong).fillna("no data")
    geojson_out = output_dir / f"{name}_edges_traffic_google.geojson"
    csv_out     = output_dir / f"{name}_edges_traffic_google.csv"
    edges.to_file(geojson_out, driver="GeoJSON")
    edges.drop(columns=["geometry", "wkt_geom"], errors="ignore").to_csv(csv_out, index=False)
    log.info(f"  GeoJSON saved   : {geojson_out.name}")
    log.info(f"  CSV saved       : {csv_out.name}")


# ── Refresh ──────────────────────────────────────────────────────────────────

def refresh_area(name: str, pbf_dir: Path, db_dir: Path, output_dir: Path) -> None:
    """
    Delete all cached files for a named area so the pipeline runs from scratch.

    What is deleted (area-specific files only):
      pbf/{name}.osm.pbf              — filtered regional PBF
      db/{name}.duckdb                — DuckDB routing network
      db/{name}.duckdb.wal            — DuckDB write-ahead log (if present)
      output/{name}_traffic.geojson   — decoded Mapbox traffic segments
      output/{name}_traffic.csv
      output/{name}_edges_traffic.geojson  — enriched edge network
      output/{name}_edges_traffic.csv

    What is NOT deleted (shared resources):
      map/*.osm.pbf    — country PBF (may be used by other areas)
      tiles/**/*.mvt   — tiles are identified by z/x/y, not by area name
      boundaries/      — input boundary files
    """
    targets = [
        pbf_dir    / f"{name}.osm.pbf",
        db_dir     / f"{name}.duckdb",
        db_dir     / f"{name}.duckdb.wal",
        output_dir / f"{name}_traffic.geojson",
        output_dir / f"{name}_traffic.csv",
        output_dir / f"{name}_edges_traffic.geojson",
        output_dir / f"{name}_edges_traffic.csv",
    ]

    log.info(f"  Refreshing cached files for '{name}':")
    deleted, skipped = 0, 0
    for path in targets:
        if path.exists():
            size_mb = path.stat().st_size / 1_048_576
            path.unlink()
            log.info(f"  Deleted : {path.name}  ({size_mb:.1f} MB)")
            deleted += 1
        else:
            log.debug(f"  Missing : {path.name} — skipped")
            skipped += 1

    log.info(f"  Result  : {deleted} file(s) deleted, {skipped} already absent")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="OSM traffic enrichment pipeline")
    group  = parser.add_mutually_exclusive_group(required=False)
    group.add_argument("--pbf",         help="Local OSM PBF file (already downloaded)")
    group.add_argument("--country-url", help="Geofabrik URL to download country PBF automatically")
    bgroup = parser.add_mutually_exclusive_group(required=True)
    bgroup.add_argument("--boundary", help="Boundary GeoJSON file (already created)")
    bgroup.add_argument("--place",    help="Place name to fetch boundary automatically "
                                           "(e.g. 'Tartu, Estonia')")
    parser.add_argument("--name",     required=True, help="Area name (used for output filenames)")
    parser.add_argument("--zoom",        type=int, default=14,
                        help="Mapbox tile zoom level (default 14)")
    parser.add_argument("--google-zoom", type=int, default=16,
                        help="Google Maps screenshot zoom level (default 16, ~2.4 m/px). "
                             "Auto-reduced if the resulting image exceeds 160 MP.")
    parser.add_argument("--config",   default=None, help="duckOSM YAML config (optional)")
    parser.add_argument("--refresh",  action="store_true",
                        help="Delete all cached files for this area before running. "
                             "The country PBF in map/ is kept.")
    parser.add_argument("--traffic-source", choices=["mapbox", "google", "both"],
                        default="both",
                        help="Traffic data source: mapbox, google, or both (default). "
                             "mapbox requires MAPBOX_ACCESS_TOKEN in .env. "
                             "google requires GOOGLE_MAPS_API_KEY in .env. "
                             "both runs Mapbox then Google sequentially.")
    args = parser.parse_args()

    use_mapbox = args.traffic_source in ("mapbox", "both")
    use_google = args.traffic_source in ("google", "both")

    token = os.environ.get("MAPBOX_ACCESS_TOKEN", "")
    if use_mapbox and (not token or not token.startswith("pk.")):
        sys.exit("ERROR: --traffic-source mapbox requires MAPBOX_ACCESS_TOKEN in .env")

    config_path    = Path(args.config).resolve() if args.config else \
                     BASE_DIR / "config" / f"{args.name}.yaml"
    boundaries_dir = BASE_DIR / "boundaries"; boundaries_dir.mkdir(exist_ok=True)

    logs_dir   = BASE_DIR / "logs";   logs_dir.mkdir(exist_ok=True)
    map_dir    = BASE_DIR / "map";    map_dir.mkdir(exist_ok=True)
    pbf_dir    = BASE_DIR / "pbf";    pbf_dir.mkdir(exist_ok=True)
    db_dir     = BASE_DIR / "db";     db_dir.mkdir(exist_ok=True)
    tiles_dir  = BASE_DIR / "tiles";  tiles_dir.mkdir(exist_ok=True)
    output_dir = BASE_DIR / "output"; output_dir.mkdir(exist_ok=True)

    log_path     = setup_logging(args.name, logs_dir)
    filtered_pbf = pbf_dir    / f"{args.name}.osm.pbf"
    db_path      = db_dir     / f"{args.name}.duckdb"
    traffic_file = output_dir / f"{args.name}_traffic.geojson"

    log.info(f"{'='*60}")
    log.info(f"  OSM Traffic Enrichment Pipeline")
    log.info(f"{'='*60}")
    log.info(f"  Area      : {args.name}")
    log.info(f"  Zoom      : {args.zoom}  (Mapbox)  |  {args.google_zoom}  (Google)")
    log.info(f"  Traffic   : {args.traffic_source}  "
             f"({'Mapbox + Google' if args.traffic_source == 'both' else args.traffic_source.title()})")
    log.info(f"  DuckDB    : {db_path}")
    log.info(f"  Started   : {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC")
    if args.refresh:
        log.info(f"  Mode      : --refresh (deleting cached files before run)")

    t_total = time.time()

    try:
        if args.refresh:
            with StepTimer("[0/4] Refresh — delete cached files"):
                refresh_area(args.name, pbf_dir, db_dir, output_dir)

        # ── Resolve boundary ──────────────────────────────────────────────
        country_code = None
        if args.place:
            with StepTimer("[0/4] Fetch boundary from place name"):
                boundary, country_code = fetch_boundary(
                    args.place, boundaries_dir, args.name)
        else:
            boundary = Path(args.boundary).resolve()
            if not boundary.exists():
                sys.exit(f"ERROR: boundary file not found: {boundary}")

        log.info(f"  Boundary  : {boundary}")

        # ── Resolve country PBF ───────────────────────────────────────────
        if args.pbf:
            pbf_input = Path(args.pbf).resolve()
            if not pbf_input.exists():
                sys.exit(f"ERROR: PBF file not found: {pbf_input}")
        else:
            # Determine URL: explicit --country-url or auto-detect from country code
            if args.country_url:
                pbf_url = args.country_url
            elif country_code:
                pbf_url = country_url_from_code(country_code)
                if pbf_url:
                    log.info(f"  Auto-detected Geofabrik URL for '{country_code}': {pbf_url}")
                else:
                    sys.exit(
                        f"ERROR: country code '{country_code}' not in the Geofabrik map.\n"
                        f"       Provide the URL manually with --country-url."
                    )
            else:
                sys.exit(
                    "ERROR: supply --pbf, --country-url, or use --place so the "
                    "country URL can be detected automatically."
                )
            with StepTimer("[0/4] Download country PBF"):
                pbf_input = download_pbf(pbf_url, map_dir)

        with StepTimer("[1/4] Filter PBF by boundary"):
            filter_pbf(pbf_input, boundary, filtered_pbf)

        with StepTimer("[2/4] Build road network (duckOSM)"):
            build_network(filtered_pbf, boundary, args.name, db_path, config_path)

        # ── Traffic steps depend on selected source ───────────────────────
        if use_mapbox:
            with StepTimer("[3/4] Fetch Mapbox traffic tiles"):
                fetch_traffic(boundary, args.zoom, token, tiles_dir, traffic_file)
            with StepTimer("[4/4] Map match → congestion_mapbox"):
                map_match(db_path, traffic_file, args.name, output_dir)

        if use_google:
            label = "[3/4]" if not use_mapbox else "[5/5]"
            with StepTimer(f"{label} Fetch & match Google → congestion_google"):
                fetch_and_match_google(db_path, boundary, args.google_zoom,
                                       args.name, output_dir)

    except Exception as e:
        log.error(f"\nPipeline FAILED: {e}")
        log.debug(traceback.format_exc())
        log.error(f"See full log: {log_path}")
        sys.exit(1)

    elapsed = time.time() - t_total
    write_summary(args.name, elapsed, log_path)
    log.info(f"\n  DuckDB  → {db_path}")
    if use_mapbox:
        log.info(f"  Mapbox  → congestion_mapbox  ({output_dir.name}/{args.name}_edges_traffic.geojson)")
    if use_google:
        log.info(f"  Google  → congestion_google  ({output_dir.name}/{args.name}_edges_traffic_google.geojson)")


if __name__ == "__main__":
    main()
