"""
osm-traffic-enrichment — combined pipeline (runs network + traffic in sequence).

NOTE: Consider using the two separate pipelines instead:
    python pipeline_network.py --config config/tartu.yaml       # build network once
    python pipeline_traffic.py --config config/tartu_traffic.yaml  # fetch traffic regularly

COMBINED USAGE:
    python pipeline.py --config config/tartu.yaml

COMMON USAGE PATTERNS:

  1. Full config file (boundary, PBF URL, zoom levels, traffic sources all in YAML):
       python pipeline.py --config config/tartu.yaml

  2. Place name only — boundary + PBF auto-detected:
       python pipeline.py --place "Tartu, Estonia" --name tartu

  3. Config file + override one source:
       python pipeline.py --config config/tartu.yaml --traffic-source mapbox

  4. Place name + explicit PBF + custom zooms:
       python pipeline.py --place "Tartu, Estonia" --name tartu \\
                          --pbf map/estonia-latest.osm.pbf \\
                          --mapbox-zoom 14 --google-zoom 16 --tomtom-zoom 14

  5. Force re-run from scratch:
       python pipeline.py --config config/tartu.yaml --refresh

ARGUMENT SUMMARY:
    --config       Full pipeline config YAML (sets boundary, zooms, sources, duckOSM options)
    --config       Full pipeline YAML — all keys below can live in the file instead.
                   CLI flags always override config values.
    --name         Area name for output files           (config: name)
    --place        Place name → auto-fetch boundary     (config: place)
    --boundary     Boundary GeoJSON path                (config: boundary_path)
    --country-url  Geofabrik PBF download URL           (config: country_url)
    --pbf          Local pre-downloaded country PBF     (config: pbf_path)
    --mapbox-zoom  Mapbox MVT tile zoom                 (config: mapbox_zoom,  default 14)
    --google-zoom  Google Maps screenshot zoom          (config: google_zoom,  default 16)
    --tomtom-zoom  TomTom PBF tile zoom                 (config: tomtom_zoom,  default 14)
    --refresh      Delete cached files before running   (config: refresh,      default false)
    --traffic-source  mapbox|google|tomtom|all          (config: traffic_sources list)

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
      [4/4] Map match + write       (writes to mapbox_congestion_history)
    --traffic-source google:
      [3/4] Fetch & match Google    (screenshot → pixel sampling → google_congestion_history)

OUTPUT:
    db/{name}.duckdb                      enriched DuckDB
      mapbox_congestion_history           Mapbox congestion time-series
      google_congestion_history           Google congestion time-series
    output/{name}_traffic_mapbox.geojson       decoded Mapbox street segments
    output/{name}_edges_traffic_mapbox.geojson  OSM edges with latest Mapbox congestion
    output/{name}_edges_traffic_mapbox.csv
    logs/pipeline_{name}_{timestamp}.log  full run log with summary
"""

import argparse
import logging
import os
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import geopandas as gpd
import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env", override=True)

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

import yaml

from scripts.mapbox_traffic import MapboxTraffic
from scripts.google_traffic import GoogleTraffic
from scripts.tomtom_traffic import TomTomTraffic
from scripts.traffic_db import TrafficDB

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

def fetch_traffic(boundary_path: Path, zoom: int, token: str,
                  tiles_dir: Path, out_file: Path) -> None:
    if out_file.exists():
        log.info(f"  Cached: {out_file.name} — skipping tile fetch")
        _step_records.append(("Fetch Mapbox traffic tiles", "skipped", 0.0))
        return
    mb = MapboxTraffic(token=token, zoom=zoom)
    gdf = mb.fetch(boundary_path, tiles_dir, out_file)
    log.info(f"  Saved   : {out_file.name}  ({len(gdf):,} segments)")
    cong_dist = gdf["congestion"].value_counts().to_dict()
    log.debug(f"  Congestion distribution: {cong_dist}")


# ── Step 4 — Map match + write to DuckDB ────────────────────────────────────

def map_match(db_path: Path, traffic_file: Path, name: str, output_dir: Path) -> None:
    mb      = MapboxTraffic(token="", zoom=0)   # token not needed for map_match
    traffic = gpd.read_file(traffic_file)
    log.info(f"  Traffic segments: {len(traffic):,}")

    edge_cong = mb.map_match(db_path, traffic)

    con = duckdb.connect(str(db_path))
    con.execute("LOAD spatial")
    n_edges = con.execute("SELECT count(*) FROM driving.edges").fetchone()[0]
    con.close()
    matched_pct = len(edge_cong) / max(n_edges, 1) * 100
    log.info(f"  Edges matched   : {len(edge_cong):,} / {n_edges:,}  ({matched_pct:.1f}%)")

    with TrafficDB(str(db_path), read_only=False) as db:
        run_id = db.write_congestion(
            edge_cong, source="mapbox", zoom=0,
            n_segments=len(traffic), boundary_name=name,
        )
    log.info(f"  mapbox_congestion_history: {len(edge_cong):,} rows (run_id={run_id})")

    # Export edges GeoJSON/CSV with congestion column for convenience
    con = duckdb.connect(str(db_path), read_only=True)
    con.execute("LOAD spatial")
    df = con.execute(
        "SELECT edge_id, highway, name, oneway, length_m, ST_AsText(geometry) wkt_geom "
        "FROM driving.edges"
    ).df()
    con.close()
    edges = gpd.GeoDataFrame(df, geometry=gpd.GeoSeries.from_wkt(df["wkt_geom"]),
                             crs="EPSG:4326").reset_index(drop=True)
    edges["congestion_mapbox"] = edges["edge_id"].map(edge_cong).fillna("no data")

    geojson_out = output_dir / f"{name}_edges_traffic_mapbox.geojson"
    csv_out     = output_dir / f"{name}_edges_traffic_mapbox.csv"
    edges.to_file(geojson_out, driver="GeoJSON")
    edges.drop(columns=["geometry", "wkt_geom"], errors="ignore").to_csv(csv_out, index=False)
    log.info(f"  GeoJSON saved   : {geojson_out.name}")
    log.info(f"  CSV saved       : {csv_out.name}")


# ── Step 3+4 (Google) — PNG tile fetch + direct pixel sampling ───────────────

def fetch_and_match_google(db_path: Path, boundary_path: Path, zoom: int,
                           name: str, output_dir: Path) -> None:
    """Render Google Maps traffic via Playwright and match to OSM edges."""
    api_key = os.environ.get("GOOGLE_MAPS_API_KEY", "")
    if not api_key:
        log.error("  GOOGLE_MAPS_API_KEY not set in .env — skipping Google traffic")
        return

    gg = GoogleTraffic(api_key=api_key, zoom=zoom)
    log.info(f"  Rendering playwright screenshot (zoom={zoom})...")
    edge_cong, total = gg.map_match(db_path, boundary_path)

    log.info(f"  Traffic pixels  : {total:,}")
    matched_pct = len(edge_cong) / max(1, 1) * 100
    log.info(f"  Edges matched   : {len(edge_cong):,}")

    if not edge_cong:
        log.warning("  No traffic pixels found — google_congestion_history not updated")
        return

    with TrafficDB(str(db_path), read_only=False) as db:
        run_id = db.write_congestion(
            edge_cong, source="google", zoom=zoom,
            n_segments=total, boundary_name=name,
        )
    log.info(f"  google_congestion_history: {len(edge_cong):,} rows (run_id={run_id})")

    # Export GeoJSON/CSV
    con = duckdb.connect(str(db_path), read_only=True)
    con.execute("LOAD spatial")
    df = con.execute(
        "SELECT edge_id, highway, name, length_m, ST_AsText(geometry) wkt_geom "
        "FROM driving.edges"
    ).df()
    con.close()
    edges = gpd.GeoDataFrame(df, geometry=gpd.GeoSeries.from_wkt(df["wkt_geom"]),
                             crs="EPSG:4326").reset_index(drop=True)
    edges["congestion_google"] = edges["edge_id"].map(edge_cong).fillna("no data")
    geojson_out = output_dir / f"{name}_edges_traffic_google.geojson"
    csv_out     = output_dir / f"{name}_edges_traffic_google.csv"
    edges.to_file(geojson_out, driver="GeoJSON")
    edges.drop(columns=["geometry", "wkt_geom"], errors="ignore").to_csv(csv_out, index=False)
    log.info(f"  GeoJSON saved   : {geojson_out.name}")
    log.info(f"  CSV saved       : {csv_out.name}")


# ── Step 3+4 (TomTom) — PBF tile fetch + map match ───────────────────────────

def fetch_and_match_tomtom(db_path: Path, boundary_path: Path, zoom: int,
                           name: str, output_dir: Path) -> None:
    """Download TomTom Traffic Flow tiles and match to OSM edges."""
    api_key = os.environ.get("TOMTOM_API_KEY", "")
    if not api_key:
        log.error("  TOMTOM_API_KEY not set in .env — skipping TomTom traffic")
        return

    tiles_dir = BASE_DIR / "tiles" / "tomtom"
    out_file  = output_dir / f"{name}_traffic_tomtom.geojson"

    tt = TomTomTraffic(api_key=api_key, zoom=zoom)
    log.info(f"  Fetching TomTom PBF tiles (zoom={zoom})...")
    gdf = tt.fetch(boundary_path, tiles_dir, out_file)
    log.info(f"  Segments        : {len(gdf):,}")
    cong_dist = gdf["congestion"].value_counts().to_dict() if len(gdf) else {}
    log.debug(f"  Congestion distribution: {cong_dist}")

    edge_cong, traffic_levels = tt.map_match(db_path, gdf)
    matched_pct = len(edge_cong) / max(1, 1) * 100
    log.info(f"  Edges matched   : {len(edge_cong):,}")

    if not edge_cong:
        log.warning("  No TomTom segments matched — tomtom_congestion_history not updated")
        return

    with TrafficDB(str(db_path), read_only=False) as db:
        run_id = db.write_congestion(
            edge_cong, source="tomtom", zoom=zoom,
            n_segments=len(gdf), boundary_name=name,
            traffic_levels=traffic_levels,
        )
    log.info(f"  tomtom_congestion_history: {len(edge_cong):,} rows (run_id={run_id})")

    # Export GeoJSON/CSV
    con = duckdb.connect(str(db_path), read_only=True)
    con.execute("LOAD spatial")
    df = con.execute(
        "SELECT edge_id, highway, name, length_m, ST_AsText(geometry) wkt_geom "
        "FROM driving.edges"
    ).df()
    con.close()
    edges = gpd.GeoDataFrame(df, geometry=gpd.GeoSeries.from_wkt(df["wkt_geom"]),
                             crs="EPSG:4326").reset_index(drop=True)
    edges["congestion_tomtom"] = edges["edge_id"].map(edge_cong).fillna("no data")
    geojson_out = output_dir / f"{name}_edges_traffic_tomtom.geojson"
    csv_out     = output_dir / f"{name}_edges_traffic_tomtom.csv"
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
      output/{name}_traffic_mapbox.geojson   — decoded Mapbox traffic segments
      output/{name}_edges_traffic_mapbox.geojson  — enriched edge network
      output/{name}_edges_traffic_mapbox.csv

    What is NOT deleted (shared resources):
      map/*.osm.pbf    — country PBF (may be used by other areas)
      tiles/**/*.mvt   — tiles are identified by z/x/y, not by area name
      boundaries/      — input boundary files
    """
    targets = [
        pbf_dir    / f"{name}.osm.pbf",
        db_dir     / f"{name}.duckdb",
        db_dir     / f"{name}.duckdb.wal",
        output_dir / f"{name}_traffic_mapbox.geojson",
        output_dir / f"{name}_edges_traffic_mapbox.geojson",
        output_dir / f"{name}_edges_traffic_mapbox.csv",
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

def _load_area_config(path: str | None) -> dict:
    """Load a pipeline config YAML file. Returns {} if path is None."""
    if path is None:
        return {}
    p = Path(path)
    if not p.exists():
        sys.exit(f"ERROR: config file not found: {p}")
    with open(p) as f:
        return yaml.safe_load(f) or {}


def main():
    parser = argparse.ArgumentParser(
        description="OSM traffic enrichment pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # ── Area config ───────────────────────────────────────────────────────
    parser.add_argument(
        "--config", default=None,
        help="Pipeline config YAML (sets name, boundary, country_url, zoom levels, "
             "traffic_sources, and duckOSM options). CLI flags override config values.",
    )

    # ── PBF source ────────────────────────────────────────────────────────
    pbf_group = parser.add_mutually_exclusive_group(required=False)
    pbf_group.add_argument("--pbf",         help="Local .osm.pbf file (already downloaded)")
    pbf_group.add_argument("--country-url", help="Geofabrik URL to download country PBF")

    # ── Boundary ──────────────────────────────────────────────────────────
    bgroup = parser.add_mutually_exclusive_group(required=False)
    bgroup.add_argument("--boundary", help="Boundary GeoJSON file")
    bgroup.add_argument("--place",    help="Place name (e.g. 'Tartu, Estonia') — "
                                          "fetches boundary + detects country PBF URL")

    # ── Area name ─────────────────────────────────────────────────────────
    parser.add_argument("--name", default=None,
                        help="Area name used for output filenames (required if not in config)")

    # ── Zoom levels ───────────────────────────────────────────────────────
    parser.add_argument("--mapbox-zoom", "--zoom", dest="mapbox_zoom", type=int, default=None,
                        help="Mapbox MVT tile zoom (default: config value or 14)")
    parser.add_argument("--google-zoom", type=int, default=None,
                        help="Google Maps screenshot zoom (default: config value or 16). "
                             "Auto-reduced if the screenshot exceeds 160 MP.")
    parser.add_argument("--tomtom-zoom", type=int, default=None,
                        help="TomTom PBF tile zoom (default: config value or 14)")

    # ── Traffic sources ───────────────────────────────────────────────────
    parser.add_argument(
        "--traffic-source",
        choices=["mapbox", "google", "tomtom", "all"],
        default=None,
        help="Which source(s) to fetch. 'all' runs every source listed in the config "
             "(or mapbox+google if no config). Default: all.",
    )

    # ── Other ─────────────────────────────────────────────────────────────
    parser.add_argument("--refresh", action="store_true",
                        help="Delete all cached files for this area before running.")

    args = parser.parse_args()

    # ── Load and merge config ─────────────────────────────────────────────
    # Resolution order for every parameter: CLI flag > config value > default
    cfg = _load_area_config(args.config)

    # name
    name = args.name or cfg.get("name")
    if not name:
        sys.exit("ERROR: --name is required (or set 'name' in --config)")

    # boundary — CLI --boundary / --place take precedence over config keys
    place_arg         = args.place    or cfg.get("place")
    boundary_arg      = args.boundary or cfg.get("boundary_path")
    if not boundary_arg and not place_arg:
        sys.exit("ERROR: supply --boundary, --place, or set boundary_path/place in --config")

    # PBF — CLI --pbf / --country-url take precedence over config keys
    pbf_arg         = args.pbf         or cfg.get("pbf_path")
    country_url_arg = args.country_url or cfg.get("country_url")

    # zoom levels
    mapbox_zoom = args.mapbox_zoom or cfg.get("mapbox_zoom", 14)
    google_zoom = args.google_zoom or cfg.get("google_zoom", 16)
    tomtom_zoom = args.tomtom_zoom or cfg.get("tomtom_zoom", 14)

    # traffic sources
    if args.traffic_source:
        if args.traffic_source == "all":
            use_sources = cfg.get("traffic_sources", ["mapbox", "google"])
        else:
            use_sources = [args.traffic_source]
    else:
        use_sources = cfg.get("traffic_sources", ["mapbox", "google"])

    use_mapbox = "mapbox" in use_sources
    use_google = "google" in use_sources
    use_tomtom = "tomtom" in use_sources

    # refresh — CLI flag always wins; config value is the default
    do_refresh = args.refresh or bool(cfg.get("refresh", False))

    # ── Validate credentials ──────────────────────────────────────────────
    token = os.environ.get("MAPBOX_ACCESS_TOKEN", "")
    if use_mapbox and (not token or not token.startswith("pk.")):
        sys.exit("ERROR: Mapbox source requires MAPBOX_ACCESS_TOKEN in .env")

    # ── duckOSM config path (network building) ────────────────────────────
    # Use --config if it contains duckOSM options; else fall back to config/{name}.yaml.
    if args.config and any(k in cfg for k in ("options", "modes", "output_path")):
        network_config_path = Path(args.config).resolve()
    else:
        candidate = BASE_DIR / "config" / f"{name}.yaml"
        network_config_path = candidate if candidate.exists() else None

    # ── Directories ───────────────────────────────────────────────────────
    boundaries_dir = BASE_DIR / "boundaries"; boundaries_dir.mkdir(exist_ok=True)
    logs_dir   = BASE_DIR / "logs";   logs_dir.mkdir(exist_ok=True)
    map_dir    = BASE_DIR / "map";    map_dir.mkdir(exist_ok=True)
    pbf_dir    = BASE_DIR / "pbf";    pbf_dir.mkdir(exist_ok=True)
    db_dir     = BASE_DIR / "db";     db_dir.mkdir(exist_ok=True)
    tiles_dir  = BASE_DIR / "tiles";  tiles_dir.mkdir(exist_ok=True)
    output_dir = BASE_DIR / "output"; output_dir.mkdir(exist_ok=True)

    log_path     = setup_logging(name, logs_dir)
    filtered_pbf = pbf_dir    / f"{name}.osm.pbf"
    db_path      = db_dir     / f"{name}.duckdb"
    traffic_file = output_dir / f"{name}_traffic_mapbox.geojson"

    sources_label = " + ".join(s.title() for s in use_sources)
    log.info(f"{'='*60}")
    log.info(f"  OSM Traffic Enrichment Pipeline")
    log.info(f"{'='*60}")
    log.info(f"  Area      : {name}")
    if use_mapbox: log.info(f"  Mapbox    : zoom {mapbox_zoom}")
    if use_google: log.info(f"  Google    : zoom {google_zoom}")
    if use_tomtom: log.info(f"  TomTom    : zoom {tomtom_zoom}")
    log.info(f"  Sources   : {sources_label}")
    log.info(f"  DuckDB    : {db_path}")
    log.info(f"  Started   : {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC")
    if do_refresh:
        log.info(f"  Mode      : refresh=true (deleting cached files before run)")
    if args.config:
        log.info(f"  Config    : {args.config}")

    t_total = time.time()

    try:
        if do_refresh:
            with StepTimer("[0] Refresh — delete cached files"):
                refresh_area(name, pbf_dir, db_dir, output_dir)

        # ── Resolve boundary ──────────────────────────────────────────────
        country_code = None
        if place_arg:
            with StepTimer("[0] Fetch boundary from place name"):
                boundary, country_code = fetch_boundary(place_arg, boundaries_dir, name)
        elif args.boundary:
            boundary = Path(args.boundary).resolve()
            if not boundary.exists():
                sys.exit(f"ERROR: boundary file not found: {boundary}")
        else:
            boundary = BASE_DIR / boundary_arg
            if not boundary.exists():
                sys.exit(f"ERROR: boundary file not found: {boundary}")

        log.info(f"  Boundary  : {boundary}")

        # ── Resolve country PBF ───────────────────────────────────────────
        if pbf_arg:
            pbf_input = Path(pbf_arg).resolve()
            if not pbf_input.exists():
                sys.exit(f"ERROR: PBF file not found: {pbf_input}")
        else:
            pbf_url = country_url_arg
            if not pbf_url and country_code:
                pbf_url = country_url_from_code(country_code)
                if pbf_url:
                    log.info(f"  Auto-detected Geofabrik URL for '{country_code}': {pbf_url}")
            if not pbf_url:
                sys.exit(
                    "ERROR: supply --pbf, --country-url, set pbf_path/country_url in --config, "
                    "or use --place/place in config for auto-detection."
                )
            with StepTimer("[0] Download country PBF"):
                pbf_input = download_pbf(pbf_url, map_dir)

        with StepTimer("[1] Filter PBF by boundary"):
            filter_pbf(pbf_input, boundary, filtered_pbf)

        with StepTimer("[2] Build road network (duckOSM)"):
            build_network(filtered_pbf, boundary, name, db_path, network_config_path)

        # ── Traffic steps ─────────────────────────────────────────────────
        step = 3
        n_traffic = sum([use_mapbox, use_google, use_tomtom])

        if use_mapbox:
            with StepTimer(f"[{step}] Fetch Mapbox traffic tiles"):
                fetch_traffic(boundary, mapbox_zoom, token, tiles_dir, traffic_file)
            step += 1
            with StepTimer(f"[{step}] Map match → mapbox_congestion_history"):
                map_match(db_path, traffic_file, name, output_dir)
            step += 1

        if use_google:
            with StepTimer(f"[{step}] Fetch & match Google → google_congestion_history"):
                fetch_and_match_google(db_path, boundary, google_zoom, name, output_dir)
            step += 1

        if use_tomtom:
            with StepTimer(f"[{step}] Fetch & match TomTom → tomtom_congestion_history"):
                fetch_and_match_tomtom(db_path, boundary, tomtom_zoom, name, output_dir)

    except Exception as e:
        log.error(f"\nPipeline FAILED: {e}")
        log.debug(traceback.format_exc())
        log.error(f"See full log: {log_path}")
        sys.exit(1)

    elapsed = time.time() - t_total
    write_summary(name, elapsed, log_path)
    log.info(f"\n  DuckDB  → {db_path}")
    if use_mapbox:
        log.info(f"  Mapbox  → mapbox_congestion_history  "
                 f"({output_dir.name}/{name}_edges_traffic_mapbox.geojson)")
    if use_google:
        log.info(f"  Google  → google_congestion_history  "
                 f"({output_dir.name}/{name}_edges_traffic_google.geojson)")
    if use_tomtom:
        log.info(f"  TomTom  → tomtom_congestion_history  "
                 f"({output_dir.name}/{name}_edges_traffic_tomtom.geojson)")


if __name__ == "__main__":
    main()
