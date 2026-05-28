"""
pipeline_traffic.py — Fetch traffic data and write to DuckDB history tables.

Run this regularly (e.g. hourly / daily) after pipeline_network.py has built the DuckDB.
Each run appends a new row to mapbox/google/tomtom_congestion_history.

The boundary polygon is read directly from the DuckDB (boundary table), so no
boundary_path is needed in the config.

USAGE:
    python pipeline_traffic.py --config config/tartu_traffic.yaml
    python pipeline_traffic.py --config config/tartu_traffic.yaml --source mapbox
    python pipeline_traffic.py --db db/tartu.duckdb --name tartu

WHAT IT DOES:
    For each enabled source in traffic_sources:
      Mapbox : download MVT tiles → spatial join → map match → mapbox_congestion_history
      Google : Playwright screenshot → pixel classify → bearing match → google_congestion_history
      TomTom : download PBF tiles → classify traffic_level → map match → tomtom_congestion_history

CONFIG FILE KEYS (all optional — CLI flags override):
    name            Area name (used for tile cache subdirs and log filenames)
    db_path         Path to the DuckDB built by pipeline_network.py
    traffic_sources List of sources to run  (default: [mapbox, google, tomtom])
    mapbox_zoom     Mapbox MVT tile zoom  (default: 14)
    google_zoom     Google screenshot zoom  (default: 16)
    tomtom_zoom     TomTom PBF tile zoom  (default: 14)
    refresh         true = delete cached tile files before running  (default: false)

    See config/traffic.template.yaml for a ready-to-copy template.
"""

import argparse
import logging
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import geopandas as gpd
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env", override=True)

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

from scripts.pipeline_utils import (
    StepTimer, load_config, setup_logging, write_summary, reset_step_records,
)
from scripts.mapbox_traffic import MapboxTraffic
from scripts.google_traffic import GoogleTraffic
from scripts.tomtom_traffic import TomTomTraffic
from scripts.traffic_db import TrafficDB

log = logging.getLogger("pipeline.traffic")


# ── Mapbox ────────────────────────────────────────────────────────────────────

def run_mapbox(db_path: Path, boundary: Path, zoom: int,
               name: str, tiles_dir: Path, output_dir: Path) -> None:
    token = os.environ.get("MAPBOX_ACCESS_TOKEN", "")
    if not token or not token.startswith("pk."):
        log.error("  MAPBOX_ACCESS_TOKEN not set or invalid — skipping Mapbox")
        return

    traffic_file = output_dir / f"{name}_traffic_mapbox.geojson"
    mb = MapboxTraffic(token=token, zoom=zoom)

    log.info(f"  Fetching Mapbox tiles (zoom={zoom})...")
    gdf = mb.fetch(boundary, tiles_dir, traffic_file)
    log.info(f"  Segments        : {len(gdf):,}")
    log.debug(f"  Distribution    : {gdf['congestion'].value_counts().to_dict()}")

    edge_cong = mb.map_match(db_path, gdf)
    log.info(f"  Edges matched   : {len(edge_cong):,}")

    with TrafficDB(str(db_path), read_only=False) as db:
        run_id = db.write_congestion(
            edge_cong, source="mapbox", zoom=zoom,
            n_segments=len(gdf), n_tiles=mb.n_tiles, boundary_name=name,
        )
    log.info(f"  mapbox_congestion_history: {len(edge_cong):,} rows (run_id={run_id})")

    _export_edges(db_path, edge_cong, "congestion_mapbox",
                  output_dir / f"{name}_edges_traffic_mapbox.geojson",
                  output_dir / f"{name}_edges_traffic_mapbox.csv")


def run_google(db_path: Path, boundary: Path, zoom: int,
               name: str, output_dir: Path) -> None:
    api_key = os.environ.get("GOOGLE_MAPS_API_KEY", "")
    if not api_key:
        log.error("  GOOGLE_MAPS_API_KEY not set — skipping Google")
        return

    gg = GoogleTraffic(api_key=api_key, zoom=zoom)
    log.info(f"  Rendering Google Maps screenshot (zoom={zoom})...")
    edge_cong, total = gg.map_match(db_path, boundary)
    log.info(f"  Traffic pixels  : {total:,}")
    log.info(f"  Edges matched   : {len(edge_cong):,}")

    if not edge_cong:
        log.warning("  No traffic pixels matched — google_congestion_history not updated")
        return

    with TrafficDB(str(db_path), read_only=False) as db:
        run_id = db.write_congestion(
            edge_cong, source="google", zoom=gg.effective_zoom,
            n_segments=total, n_tiles=gg.n_tiles, boundary_name=name,
        )
    if gg.effective_zoom != zoom:
        log.info(f"  Note: requested zoom={zoom}, rendered at zoom={gg.effective_zoom} (memory cap)")
    log.info(f"  google_congestion_history: {len(edge_cong):,} rows (run_id={run_id})")

    _export_edges(db_path, edge_cong, "congestion_google",
                  output_dir / f"{name}_edges_traffic_google.geojson",
                  output_dir / f"{name}_edges_traffic_google.csv")


def run_tomtom(db_path: Path, boundary: Path, zoom: int,
               name: str, tiles_dir: Path, output_dir: Path) -> None:
    api_key = os.environ.get("TOMTOM_API_KEY", "")
    if not api_key:
        log.error("  TOMTOM_API_KEY not set — skipping TomTom")
        return

    traffic_file = output_dir / f"{name}_traffic_tomtom.geojson"
    tt = TomTomTraffic(api_key=api_key, zoom=zoom)

    log.info(f"  Fetching TomTom PBF tiles (zoom={zoom})...")
    gdf = tt.fetch(boundary, tiles_dir, traffic_file)
    log.info(f"  Segments        : {len(gdf):,}")

    edge_cong, traffic_levels = tt.map_match(db_path, gdf)
    log.info(f"  Edges matched   : {len(edge_cong):,}")

    if not edge_cong:
        log.warning("  No TomTom segments matched — tomtom_congestion_history not updated")
        return

    with TrafficDB(str(db_path), read_only=False) as db:
        run_id = db.write_congestion(
            edge_cong, source="tomtom", zoom=zoom,
            n_segments=len(gdf), n_tiles=tt.n_tiles, boundary_name=name,
            traffic_levels=traffic_levels,
        )
    log.info(f"  tomtom_congestion_history: {len(edge_cong):,} rows (run_id={run_id})")

    _export_edges(db_path, edge_cong, "congestion_tomtom",
                  output_dir / f"{name}_edges_traffic_tomtom.geojson",
                  output_dir / f"{name}_edges_traffic_tomtom.csv")


def _export_edges(db_path: Path, edge_cong: dict, col: str,
                  geojson_out: Path, csv_out: Path) -> None:
    con = duckdb.connect(str(db_path), read_only=True)
    con.execute("INSTALL spatial")
    con.execute("LOAD spatial")
    df = con.execute(
        "SELECT edge_id, highway, name, length_m, ST_AsText(geometry) wkt_geom "
        "FROM driving.edges"
    ).df()
    con.close()
    edges = gpd.GeoDataFrame(df, geometry=gpd.GeoSeries.from_wkt(df["wkt_geom"]),
                             crs="EPSG:4326").reset_index(drop=True)
    edges[col] = edges["edge_id"].map(edge_cong).fillna("no data")
    edges.to_file(geojson_out, driver="GeoJSON")
    edges.drop(columns=["geometry", "wkt_geom"], errors="ignore").to_csv(csv_out, index=False)
    log.info(f"  GeoJSON saved   : {geojson_out.name}")
    log.info(f"  CSV saved       : {csv_out.name}")


# ── Refresh — clear tile cache ────────────────────────────────────────────────

def refresh_tiles(name: str, tiles_dir: Path, output_dir: Path) -> None:
    """Delete cached tile files and traffic GeoJSONs (does NOT touch the DuckDB)."""
    targets = [
        *list((tiles_dir / "traffic").glob("*.mvt")),
        *list((tiles_dir / "streets").glob("*.mvt")),
        *list((tiles_dir / "tomtom").glob("*.pbf")),
        output_dir / f"{name}_traffic_mapbox.geojson",
        output_dir / f"{name}_traffic_tomtom.geojson",
    ]
    deleted = 0
    for path in targets:
        if path.exists():
            path.unlink()
            deleted += 1
    log.info(f"  {deleted} cached tile/traffic file(s) deleted")


# ── Boundary from DuckDB ─────────────────────────────────────────────────────

def _get_boundary_geojson(db_path: Path, boundaries_dir: Path, name: str) -> Path:
    """
    Return path to boundary GeoJSON.
    Falls back to extracting from the DuckDB boundary table if the file is missing.
    """
    path = boundaries_dir / f"{name}.geojson"
    if path.exists():
        return path

    # Extract from DuckDB
    con = duckdb.connect(str(db_path), read_only=True)
    con.execute("INSTALL spatial")
    con.execute("LOAD spatial")
    df = con.execute("SELECT ST_AsText(geom) AS wkt FROM boundary LIMIT 1").df()
    con.close()

    if df.empty:
        raise RuntimeError(
            f"No boundary in {db_path} and {path} does not exist. "
            f"Run pipeline_network.py first."
        )

    gdf = gpd.GeoDataFrame(
        geometry=gpd.GeoSeries.from_wkt(df["wkt"]), crs="EPSG:4326"
    )
    boundaries_dir.mkdir(exist_ok=True)
    gdf.to_file(path, driver="GeoJSON")
    log.info(f"  Boundary extracted from DuckDB → {path.name}")
    return path


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Fetch traffic data and write to DuckDB history tables.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--config", default=None,
                        help="Traffic config YAML (see config/traffic.template.yaml)")
    parser.add_argument("--db",   default=None,
                        help="Path to DuckDB file  (config: db_path)")
    parser.add_argument("--name", default=None,
                        help="Area name for logs and tile dirs  (config: name)")
    parser.add_argument("--source",
                        choices=["mapbox", "google", "tomtom", "all"],
                        default=None,
                        help="Run only one source; 'all' uses traffic_sources from config")
    parser.add_argument("--mapbox-zoom", type=int, default=None,
                        help="Mapbox tile zoom  (config: mapbox_zoom, default 14)")
    parser.add_argument("--google-zoom", type=int, default=None,
                        help="Google screenshot zoom  (config: google_zoom, default 16)")
    parser.add_argument("--tomtom-zoom", type=int, default=None,
                        help="TomTom tile zoom  (config: tomtom_zoom, default 14)")
    parser.add_argument("--refresh", action="store_true",
                        help="Delete cached tile files before running  (config: refresh)")

    args = parser.parse_args()
    cfg  = load_config(args.config)

    # ── Resolve parameters ────────────────────────────────────────────────
    name = args.name or cfg.get("name")
    if not name:
        sys.exit("ERROR: --name required (or set 'name' in config)")

    db_path = Path(args.db or cfg.get("db_path") or f"db/{name}.duckdb")
    if not db_path.exists():
        sys.exit(f"ERROR: DuckDB not found: {db_path}  "
                 f"(run pipeline_network.py first, or set db_path in config)")

    # Boundary is read from the DuckDB boundary table (written by pipeline_network.py)
    boundary = _get_boundary_geojson(db_path, BASE_DIR / "boundaries", name)

    mapbox_zoom = args.mapbox_zoom or cfg.get("mapbox_zoom", 14)
    google_zoom = args.google_zoom or cfg.get("google_zoom", 16)
    tomtom_zoom = args.tomtom_zoom or cfg.get("tomtom_zoom", 14)
    do_refresh  = args.refresh or bool(cfg.get("refresh", False))

    if args.source and args.source != "all":
        use_sources = [args.source]
    else:
        use_sources = cfg.get("traffic_sources", ["mapbox", "google", "tomtom"])

    # ── Directories ───────────────────────────────────────────────────────
    logs_dir   = BASE_DIR / "logs";   logs_dir.mkdir(exist_ok=True)
    tiles_dir  = BASE_DIR / "tiles";  tiles_dir.mkdir(exist_ok=True)
    output_dir = BASE_DIR / "output"; output_dir.mkdir(exist_ok=True)

    reset_step_records()
    log_path = setup_logging(name, logs_dir, "pipeline.traffic")

    sources_label = " + ".join(s.title() for s in use_sources)
    log.info(f"{'='*60}")
    log.info(f"  Traffic Pipeline")
    log.info(f"{'='*60}")
    log.info(f"  Area      : {name}")
    log.info(f"  DuckDB    : {db_path}")
    log.info(f"  Boundary  : {boundary.name} (from DuckDB boundary table)")
    log.info(f"  Sources   : {sources_label}")
    if "mapbox" in use_sources: log.info(f"  Mapbox    : zoom {mapbox_zoom}")
    if "google" in use_sources: log.info(f"  Google    : zoom {google_zoom}")
    if "tomtom" in use_sources: log.info(f"  TomTom    : zoom {tomtom_zoom}")
    log.info(f"  Refresh   : {do_refresh}")
    log.info(f"  Started   : {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC")
    if args.config:
        log.info(f"  Config    : {args.config}")

    t_total = time.time()

    try:
        if do_refresh:
            with StepTimer("[0] Refresh — delete cached tile files"):
                refresh_tiles(name, tiles_dir, output_dir)

        step = 1
        if "mapbox" in use_sources:
            with StepTimer(f"[{step}] Mapbox → mapbox_congestion_history"):
                run_mapbox(db_path, boundary, mapbox_zoom, name, tiles_dir, output_dir)
            step += 1

        if "google" in use_sources:
            with StepTimer(f"[{step}] Google → google_congestion_history"):
                run_google(db_path, boundary, google_zoom, name, output_dir)
            step += 1

        if "tomtom" in use_sources:
            with StepTimer(f"[{step}] TomTom → tomtom_congestion_history"):
                run_tomtom(db_path, boundary, tomtom_zoom, name,
                           tiles_dir / "tomtom", output_dir)

    except Exception as e:
        log.error(f"\nPipeline FAILED: {e}")
        log.debug(traceback.format_exc())
        log.error(f"See: {log_path}")
        sys.exit(1)

    write_summary(name, time.time() - t_total, log_path)
    log.info(f"\n  DuckDB → {db_path}")

    if os.environ.get("MOTHERDUCK_TOKEN"):
        log.info("  Syncing to MotherDuck...")
        try:
            from scripts.motherduck_sync import sync as md_sync
            md_sync(db_path)
            log.info("  MotherDuck sync complete.")
        except Exception as e:
            log.warning(f"  MotherDuck sync failed (non-fatal): {e}")


if __name__ == "__main__":
    main()
