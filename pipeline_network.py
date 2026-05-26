"""
pipeline_network.py — Build OSM road network and H3 boundary cells.

Run this once per area (or when the network needs to be rebuilt).
The result is a DuckDB file in db/ that the traffic pipeline reads from.

USAGE:
    python pipeline_network.py --config config/tartu.yaml
    python pipeline_network.py --place "Tartu, Estonia" --name tartu
    python pipeline_network.py --config config/tartu.yaml --refresh

WHAT IT DOES:
    [0] (optional) Refresh — delete cached PBF + DuckDB
    [0] (optional) Fetch boundary from place name via Nominatim
    [0] (optional) Download country PBF from Geofabrik
    [1] Filter country PBF to boundary extent (osmium extract)
    [2] Build road network via duckOSM → DuckDB
    [3] (optional) Generate H3 boundary cells for each resolution in h3_resolutions

CONFIG FILE KEYS (all optional — CLI flags override):
    name            Area name for output files
    place           Place name → auto-fetch boundary + detect country PBF
    boundary_path   Path to existing boundary GeoJSON
    country_url     Geofabrik country PBF download URL
    country_pbf     Path to a local country PBF (before filtering)
    refresh         true = delete cached files before running  (default: false)
    h3_resolutions  List of H3 resolutions to generate  (e.g. [6, 7, 8])
    output_path     duckOSM output directory  (default: db)
    options         duckOSM build options (build_graph, h3_indexing, etc.)
    modes           duckOSM routing modes  (default: [driving, walking, cycling])
"""

import argparse
import json
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
import pandas as pd
import requests
from dotenv import load_dotenv
from shapely.geometry import box, mapping, shape

load_dotenv(Path(__file__).parent / ".env", override=True)

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

from scripts.pipeline_utils import (
    StepTimer, load_config, setup_logging, write_summary,
    reset_step_records, country_url_from_code,
)

log = logging.getLogger("pipeline.network")


# ── Step 0a — Fetch boundary ─────────────────────────────────────────────────

def fetch_boundary(place_name: str, boundaries_dir: Path, name: str
                   ) -> tuple[Path, str | None]:
    out_path = boundaries_dir / f"{name}.geojson"
    log.info(f"  Place   : {place_name}")

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

    best = next((r for r in results if r["osm_type"] == "relation"
                 and r.get("geojson", {}).get("type") in ("Polygon", "MultiPolygon")),
                results[0])
    country_code = best.get("address", {}).get("country_code")
    log.info(f"  Found   : {best['display_name'][:70]}")
    log.info(f"  Country : {country_code or 'unknown'}")

    if out_path.exists():
        log.info(f"  Cached  : {out_path.name} already exists — skipping save")
        return out_path, country_code

    if not best.get("geojson"):
        raise ValueError(f"No polygon returned for '{place_name}'")

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
        return out_path

    log.info(f"  Source : {url}")
    with requests.get(url, stream=True, timeout=30) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        log.info(f"  Size   : {total/1_048_576:.0f} MB")
        downloaded = 0
        with open(out_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8 * 1024 * 1024):
                f.write(chunk)
                downloaded += len(chunk)
                if total:
                    print(f"  {downloaded/1_048_576:.0f} / {total/1_048_576:.0f} MB "
                          f"({downloaded/total*100:.0f}%)", end="\r")
    print()
    log.info(f"  Saved  : {out_path.name}  ({out_path.stat().st_size/1_048_576:.0f} MB)")
    return out_path


# ── Step 1 — Filter PBF ──────────────────────────────────────────────────────

def filter_pbf(pbf_input: Path, boundary: Path, output: Path) -> None:
    if output.exists():
        log.info(f"  Cached: {output.name} — skipping osmium extract")
        return

    log.info(f"  Input   : {pbf_input.name}  ({pbf_input.stat().st_size/1_048_576:.0f} MB)")
    log.info(f"  Boundary: {boundary.name}")

    cmd = ["osmium", "extract", "--polygon", str(boundary.resolve()),
           "--output", str(output.resolve()), "--overwrite", str(pbf_input.resolve())]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"osmium extract failed:\n{result.stderr}")

    log.info(f"  Result  : {output.name}  ({output.stat().st_size/1_048_576:.2f} MB)")


# ── Step 2 — Build network ───────────────────────────────────────────────────

def build_network(pbf: Path, boundary: Path, name: str, db_path: Path,
                  config_path: Path | None,
                  modes_override: list[str] | None = None,
                  h3_resolutions_hint: list[int] | None = None) -> None:
    if db_path.exists():
        size_mb = db_path.stat().st_size / 1_048_576
        # Check which schemas (modes) are present so the user knows what was built
        try:
            import duckdb as _ddb
            _con = _ddb.connect(str(db_path), read_only=True)
            modes_present = [r[0] for r in _con.execute(
                "SELECT DISTINCT table_schema FROM information_schema.tables "
                "WHERE table_schema NOT IN ('main','raw') ORDER BY 1"
            ).fetchall()]
            _con.close()
        except Exception:
            modes_present = ["?"]
        log.info(f"  Cached: {db_path.name}  ({size_mb:.1f} MB) — skipping duckOSM")
        log.info(f"  Modes present: {modes_present}  (use --refresh to rebuild with new modes)")
        return

    try:
        from duckosm import DuckOSM, Config
    except ImportError:
        raise ImportError("duckOSM not found. Install: pip install -e ../duckOSM")

    # Always use Config.from_args so the pipeline controls pbf_path and output_path.
    # We read modes + options from the config YAML if available, but never let
    # duckOSM look for pbf_path inside the YAML (it won't be there).
    if config_path and config_path.exists():
        import yaml as _yaml
        with open(config_path) as _f:
            _cfg = _yaml.safe_load(_f) or {}
        opts         = _cfg.get("options", {})
        modes        = _cfg.get("modes", ["driving", "walking", "cycling"])
        cfg_h3_resolutions = _cfg.get("h3_resolutions", [])
        log.info(f"  Config  : {config_path}")
    else:
        opts               = {}
        modes              = ["driving", "walking", "cycling"]
        cfg_h3_resolutions = []
        log.info(f"  Config  : programmatic defaults")

    # CLI --modes overrides config
    if modes_override:
        modes = modes_override

    # h3_resolution for edge indexing: explicit options value takes priority,
    # otherwise derive from the finest resolution in h3_resolutions so that
    # edge cell indices always match the boundary_cells table.
    all_resolutions = h3_resolutions_hint or cfg_h3_resolutions
    if "h3_resolution" in opts:
        h3_resolution = opts["h3_resolution"]
    elif all_resolutions:
        h3_resolution = max(all_resolutions)
        log.info(f"  h3_resolution derived from max(h3_resolutions) = {h3_resolution}")
    else:
        h3_resolution = 8  # safe default

    log.info(f"  Modes         : {modes}")
    log.info(f"  h3_resolution : {h3_resolution}")

    config = Config.from_args(
        pbf_path=str(pbf),
        output_path=str(db_path.parent),
        name=name,
        boundary_path=str(boundary),
        modes=modes,
        build_graph=opts.get("build_graph", True),
        h3_indexing=opts.get("h3_indexing", True),
        h3_resolution=h3_resolution,
        simplify=opts.get("simplify", True),
        process_speeds=opts.get("process_speeds", True),
        extract_restrictions=opts.get("extract_restrictions", True),
        calculate_costs=opts.get("calculate_costs", True),
    )

    result_path = DuckOSM(config).run()
    log.info(f"  Result  : {result_path.name}  ({result_path.stat().st_size/1_048_576:.1f} MB)")

    con = duckdb.connect(str(result_path), read_only=True)
    for mode in ["driving", "walking", "cycling"]:
        try:
            n = con.execute(f"SELECT count(*) FROM {mode}.edges").fetchone()[0]
            log.info(f"  Edges   : {mode:8s} → {n:,}")
        except Exception:
            pass
    con.close()


# ── Step 3 — H3 boundary cells ───────────────────────────────────────────────

def generate_h3_cells(boundary_path: Path, db_path: Path,
                      resolutions: list[int], output_dir: Path) -> None:
    """Generate H3 cells for each resolution and write to boundary_cells table."""
    try:
        import h3
    except ImportError:
        raise ImportError("h3 not installed — run: pip install h3")

    with open(boundary_path) as f:
        geojson_data = json.load(f)
    geometry    = geojson_data["features"][0]["geometry"]
    shapely_poly = shape(geometry)

    def swap(coords):
        return [(lat, lon) for lon, lat in coords]

    con = duckdb.connect(str(db_path))
    con.execute("LOAD spatial")
    con.execute("""
        CREATE TABLE IF NOT EXISTS boundary_cells (
            h3_id      VARCHAR PRIMARY KEY,
            resolution INTEGER,
            geometry   GEOMETRY
        )
    """)

    for res in resolutions:
        buf_deg  = h3.average_hexagon_edge_length(res, "km") / 111.0 * 0.5
        buffered = mapping(shapely_poly.buffer(buf_deg))
        outer    = swap(buffered["coordinates"][0])
        holes    = [swap(r) for r in buffered["coordinates"][1:]]
        cells    = list(h3.polygon_to_cells(h3.LatLngPoly(outer, *holes), res))

        cell_rows = []
        for cell in cells:
            coords = [(lon, lat) for lat, lon in h3.cell_to_boundary(cell)]
            coords.append(coords[0])
            wkt = "POLYGON ((" + ", ".join(f"{x} {y}" for x, y in coords[:-1]) \
                  + f", {coords[0][0]} {coords[0][1]}" + "))"
            cell_rows.append({"h3_id": cell, "resolution": res, "geometry_wkt": wkt})

        con.execute(f"DELETE FROM boundary_cells WHERE resolution = {res}")
        df = pd.DataFrame(cell_rows)
        con.register("_cells", df)
        con.execute("INSERT INTO boundary_cells "
                    "SELECT h3_id, resolution, ST_GeomFromText(geometry_wkt) FROM _cells")
        con.unregister("_cells")

        # Save CSV + GeoJSON outputs
        output_dir.mkdir(exist_ok=True)
        pd.DataFrame({"h3_id": cells}).to_csv(
            output_dir / f"{db_path.stem}_{res}_h3_cells.csv", index=False)
        features = [{"type": "Feature",
                     "geometry": {"type": "Polygon", "coordinates": [
                         [(lon, lat) for lat, lon in h3.cell_to_boundary(c)] + [
                             list(h3.cell_to_boundary(c))[0][::-1]]]},
                     "properties": {"h3_id": c, "resolution": res}}
                    for c in cells]
        with open(output_dir / f"{db_path.stem}_{res}_h3_cells.geojson", "w") as f:
            json.dump({"type": "FeatureCollection", "features": features}, f)

        log.info(f"  Resolution {res}: {len(cells)} cells → DuckDB + CSV + GeoJSON")

    summary = con.execute(
        "SELECT resolution, count(*) AS cells FROM boundary_cells GROUP BY 1 ORDER BY 1"
    ).df()
    con.close()
    log.info(f"  boundary_cells total: {summary.to_dict('records')}")


# ── Config Generation ────────────────────────────────────────────────────────

def save_default_configs(name: str, boundary_path: Path, country_url: str | None,
                         country_pbf: Path | None, resolutions: list[int]) -> None:
    """Automatically create default network and traffic configs if they don't exist."""
    config_dir = BASE_DIR / "config"
    config_dir.mkdir(exist_ok=True)

    # 1. Network config
    net_cfg_path = config_dir / f"{name}.yaml"
    if not net_cfg_path.exists():
        log.info(f"  Generating network config: config/{name}.yaml")
        lines = [
            f"# Network config for {name}",
            f"# Auto-generated during pipeline run on {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC",
            "",
            f"name: {name}",
            f"boundary_path: boundaries/{boundary_path.name}",
        ]
        if country_pbf:
            try:
                rel_pbf = country_pbf.relative_to(BASE_DIR)
                lines.append(f"country_pbf: {rel_pbf}")
            except ValueError:
                lines.append(f"country_pbf: {country_pbf}")
        elif country_url:
            lines.append(f"country_url: {country_url}")
        else:
            lines.append("country_url: https://download.geofabrik.de/europe/sweden-latest.osm.pbf")

        lines.extend([
            "refresh: false",
            "",
            "h3_resolutions:",
        ])
        for res in resolutions:
            lines.append(f"  - {res}")

        lines.extend([
            "",
            "output_path: db",
            "",
            "options:",
            "  build_graph: true",
            "  h3_indexing: true",
            "  simplify: true",
            "  process_speeds: true",
            "  extract_restrictions: true",
            "  calculate_costs: true",
            "",
            "modes:",
            "  - driving",
            "  - walking",
            "  - cycling",
        ])
        with open(net_cfg_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        log.info(f"  Saved network config to {net_cfg_path}")

    # 2. Traffic config
    traf_cfg_path = config_dir / f"{name}_traffic.yaml"
    if not traf_cfg_path.exists():
        log.info(f"  Generating traffic config: config/{name}_traffic.yaml")
        lines = [
            f"# Traffic config for {name}",
            f"# Auto-generated during pipeline run on {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC",
            "",
            f"name: {name}",
            f"db_path: db/{name}.duckdb",
            "refresh: false",
            "",
            "traffic_sources:",
            "  - mapbox",
            "  - google",
            "  - tomtom",
            "",
            "mapbox_zoom: 14",
            "google_zoom: 16",
            "tomtom_zoom: 14",
        ]
        with open(traf_cfg_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        log.info(f"  Saved traffic config to {traf_cfg_path}")


# ── Refresh ───────────────────────────────────────────────────────────────────

def refresh_area(name: str, pbf_dir: Path, db_dir: Path) -> None:
    targets = [
        pbf_dir / f"{name}.osm.pbf",
        db_dir  / f"{name}.duckdb",
        db_dir  / f"{name}.duckdb.wal",
    ]
    deleted = 0
    for path in targets:
        if path.exists():
            size_mb = path.stat().st_size / 1_048_576
            path.unlink()
            log.info(f"  Deleted : {path.name}  ({size_mb:.1f} MB)")
            deleted += 1
    log.info(f"  {deleted} file(s) deleted")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Build OSM road network + H3 boundary cells into DuckDB.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--config", default=None,
                        help="Network config YAML (duckOSM format + pipeline extensions)")

    pbf_group = parser.add_mutually_exclusive_group()
    pbf_group.add_argument("--pbf",         help="Local country .osm.pbf file")
    pbf_group.add_argument("--country-url", help="Geofabrik PBF download URL")

    bgroup = parser.add_mutually_exclusive_group()
    bgroup.add_argument("--boundary", help="Boundary GeoJSON path")
    bgroup.add_argument("--place",    help="Place name — auto-fetches boundary + country PBF")

    parser.add_argument("--name",    default=None, help="Area name (config: name)")
    parser.add_argument("--modes", nargs="+",
                        metavar="MODE",
                        default=None,
                        help="Routing modes to build, e.g. --modes driving walking cycling "
                             "(config: modes, default: driving walking cycling)")
    parser.add_argument("--h3-resolutions", nargs="+", type=int,
                        metavar="RES",
                        default=None,
                        dest="h3_resolutions",
                        help="H3 resolutions for boundary_cells, e.g. --h3-resolutions 6 7 8 "
                             "(config: h3_resolutions, default: 6 7 8)")
    parser.add_argument("--refresh", action="store_true",
                        help="Delete cached PBF + DuckDB before running (config: refresh)")
    parser.add_argument("--no-h3",  action="store_true",
                        help="Skip H3 boundary cell generation")

    args = parser.parse_args()
    cfg  = load_config(args.config)

    # ── Resolve parameters (CLI > config > default) ───────────────────────
    name = args.name or cfg.get("name")
    if not name:
        sys.exit("ERROR: --name required (or set 'name' in --config)")

    place_arg    = args.place    or cfg.get("place")
    boundary_arg = args.boundary or cfg.get("boundary_path")
    if not place_arg and not boundary_arg:
        sys.exit("ERROR: supply --place, --boundary, or set place/boundary_path in config")

    # country_pbf: local country PBF (distinct from duckOSM's pbf_path = filtered PBF)
    pbf_arg         = args.pbf         or cfg.get("country_pbf")
    country_url_arg = args.country_url or cfg.get("country_url")
    do_refresh      = args.refresh or bool(cfg.get("refresh", False))
    # CLI --h3-resolutions > config h3_resolutions > default [6, 7, 8]
    resolutions     = args.h3_resolutions or cfg.get("h3_resolutions", [6, 7, 8])

    # ── Directories ───────────────────────────────────────────────────────
    boundaries_dir = BASE_DIR / "boundaries"; boundaries_dir.mkdir(exist_ok=True)
    logs_dir       = BASE_DIR / "logs";       logs_dir.mkdir(exist_ok=True)
    map_dir        = BASE_DIR / "map";        map_dir.mkdir(exist_ok=True)
    pbf_dir        = BASE_DIR / "pbf";        pbf_dir.mkdir(exist_ok=True)
    db_dir         = BASE_DIR / "db";         db_dir.mkdir(exist_ok=True)
    output_dir     = BASE_DIR / "output";     output_dir.mkdir(exist_ok=True)

    reset_step_records()
    log_path     = setup_logging(name, logs_dir, "pipeline.network")
    filtered_pbf = pbf_dir / f"{name}.osm.pbf"
    db_path      = db_dir  / f"{name}.duckdb"

    log.info(f"{'='*60}")
    log.info(f"  Network Pipeline")
    log.info(f"{'='*60}")
    log.info(f"  Area        : {name}")
    log.info(f"  DuckDB      : {db_path}")
    log.info(f"  H3 res.     : {resolutions or '(none)'}")
    log.info(f"  Refresh     : {do_refresh}")
    log.info(f"  Started     : {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC")
    if args.config:
        log.info(f"  Config      : {args.config}")

    t_total = time.time()

    try:
        if do_refresh:
            with StepTimer("[0] Refresh — delete cached files"):
                refresh_area(name, pbf_dir, db_dir)

        # ── Boundary ─────────────────────────────────────────────────────
        country_code = None
        if place_arg:
            with StepTimer("[0] Fetch boundary from place name"):
                boundary, country_code = fetch_boundary(place_arg, boundaries_dir, name)
        else:
            boundary = BASE_DIR / boundary_arg if not Path(boundary_arg).is_absolute() \
                       else Path(boundary_arg)
            if not boundary.exists():
                sys.exit(f"ERROR: boundary not found: {boundary}")

        log.info(f"  Boundary    : {boundary}")

        # ── Country PBF ──────────────────────────────────────────────────
        pbf_url = None
        if pbf_arg:
            pbf_input = Path(pbf_arg) if Path(pbf_arg).is_absolute() \
                        else BASE_DIR / pbf_arg
            if not pbf_input.exists():
                sys.exit(f"ERROR: PBF not found: {pbf_input}")
        else:
            pbf_url = country_url_arg
            if not pbf_url and country_code:
                pbf_url = country_url_from_code(country_code)
                if pbf_url:
                    log.info(f"  Auto-detected Geofabrik URL: {pbf_url}")
            if not pbf_url:
                sys.exit(
                    "ERROR: supply --pbf, --country-url, or set pbf_path/country_url in config "
                    "(or use --place so the URL is auto-detected)."
                )
            with StepTimer("[0] Download country PBF"):
                pbf_input = download_pbf(pbf_url, map_dir)

        # build_network reads modes + options from config YAML directly
        network_cfg = Path(args.config).resolve() if args.config else \
                      (BASE_DIR / "config" / f"{name}.yaml"
                       if (BASE_DIR / "config" / f"{name}.yaml").exists() else None)

        with StepTimer("[1] Filter PBF by boundary"):
            filter_pbf(pbf_input, boundary, filtered_pbf)

        with StepTimer("[2] Build road network (duckOSM)"):
            build_network(filtered_pbf, boundary, name, db_path, network_cfg,
                          modes_override=args.modes,
                          h3_resolutions_hint=resolutions or None)

        if resolutions and not args.no_h3:
            with StepTimer("[3] Generate H3 boundary cells"):
                generate_h3_cells(boundary, db_path, resolutions, output_dir)

        # ── Save default configs if they do not exist ─────────────────────
        save_default_configs(name, boundary, pbf_url, pbf_input if pbf_arg else None, resolutions)

    except Exception as e:
        log.error(f"\nPipeline FAILED: {e}")
        log.debug(traceback.format_exc())
        log.error(f"See: {log_path}")
        sys.exit(1)

    write_summary(name, time.time() - t_total, log_path)
    log.info(f"\n  DuckDB → {db_path}")


if __name__ == "__main__":
    main()
