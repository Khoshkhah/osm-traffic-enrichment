# osm-traffic-enrichment

[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://github.com/codespaces/new?repo=Khoshkhah/osm-traffic-enrichment)
[![Sensor Selector (Cloud)](https://img.shields.io/badge/Sensor_Selector-Cloud_%E2%86%97-blue?style=flat)](https://khoshkhah.github.io/osm-traffic-enrichment/sensor_selector_cloud.html)

An end-to-end pipeline that enriches an OSM road network with **real-time traffic congestion**
from three independent sources — **Mapbox**, **Google Maps**, and **TomTom** — producing a
routable **DuckDB** database with per-source historical congestion tables.

---

## Traffic sources

| Source | Method | Coverage | Raw value |
|---|---|---|---|
| **Mapbox** | MVT vector tiles → geometric map match | Near 100% — free-flow roads get `low` | Pre-classified segment |
| **Google Maps** | Playwright screenshot → CIE76 pixel classification → bearing-based match | Partial — only congested roads colored | 4-color pixel |
| **TomTom** | PBF flow tiles → `traffic_level` classification → geometric match | Road network coverage | Float 0.0–1.0 (stored raw) |

`no data` from Google means the road is flowing normally (Google does not color free-flow roads).
TomTom raw `traffic_level` is stored in `tomtom_congestion_history` so thresholds can be re-applied without re-fetching.

---

## Architecture

Two separate pipelines with distinct responsibilities:

```
pipeline_network.py          (run once per area)
    │
    ├── Fetch boundary       (Nominatim API or existing GeoJSON)
    ├── Download country PBF (Geofabrik, cached)
    ├── Filter PBF           (osmium extract → pbf/{name}.osm.pbf)
    ├── Build network        (duckOSM → db/{name}.duckdb)
    └── Generate H3 cells    (resolutions 6, 7, 8 → boundary_cells table)

pipeline_traffic.py          (run regularly — hourly / daily)
    │
    ├── Mapbox  → fetch MVT tiles → map match → mapbox_congestion_history
    ├── Google  → Playwright screenshot → pixel classify → google_congestion_history
    └── TomTom  → fetch PBF tiles → traffic_level classify → tomtom_congestion_history
```

---

## Project structure

```
osm-traffic-enrichment/
├── pipeline_network.py              # Build network + H3 cells (run once)
├── pipeline_traffic.py              # Fetch traffic from all sources (run regularly)
│
├── scripts/
│   ├── sensor_selector.py           # Interactive GIS dashboard HTTP server
│   ├── sensor_selector.html         # Leaflet-based spatial selection interface
│   ├── motherduck_sync.py           # Sync local .duckdb files to MotherDuck cloud
│   ├── traffic_db.py                # Python library for querying the DuckDB
│   ├── mapbox_traffic.py            # MapboxTraffic: fetch tiles + map match
│   ├── google_traffic.py            # GoogleTraffic: screenshot + pixel match
│   ├── tomtom_traffic.py            # TomTomTraffic: fetch PBF + map match
│   ├── pipeline_utils.py            # Shared logging, timing, config loading
│   └── filter_pbf.py                # osmium extract wrapper
│
├── config/
│   ├── network.template.yaml        # Template for network configs
│   ├── traffic.template.yaml        # Template for traffic configs
│   └── {area}.yaml + {area}_traffic.yaml  # Per-area configs — auto-generated on first network run
│
├── notebook/
│   ├── 0_get_boundary.ipynb         # Create boundary GeoJSON
│   ├── 1_filter_pbf.ipynb           # Download PBF + osmium extract
│   ├── 2_build_network.ipynb        # duckOSM → DuckDB
│   ├── 3a_fetch_traffic_mapbox.ipynb# Mapbox tiles → history table
│   ├── 3b_fetch_traffic_google.ipynb# Playwright render → history table
│   ├── 3c_fetch_traffic_tomtom.ipynb# TomTom PBF → history table
│   ├── 4_polygon_to_cells.ipynb     # Boundary → H3 cells (multi-resolution)
│   └── 5_query_duckdb.ipynb         # Queries + Folium visualizations
│
├── docs/
│   ├── pipeline_cli.md              # CLI reference for both pipelines
│   ├── database_schema.md           # DuckDB table and column reference
│   ├── traffic_db.md                # scripts/traffic_db.py API reference
│   ├── traffic_status.md            # Congestion level definitions
│   ├── sensor_selector_cloud.html   # Cloud-hosted Sensor Selector webpage
│   ├── map_road_color.md            # Multi-base-layer road styling reference
│   ├── osm_highway_styles.md        # OSM Carto highway style reference
│   └── selected_edge_color.md       # Selected-edge UI/UX color spec
│
└── boundaries/                      # Area boundary polygons (one per area)
```

**Generated at runtime (git-ignored):**
```
map/    ← country PBF files
pbf/    ← filtered regional PBF files
db/     ← DuckDB databases
tiles/  ← cached Mapbox .mvt + TomTom .pbf tiles
output/ ← GeoJSON + CSV exports
logs/   ← timestamped run logs
```

---

## Prerequisites

| Requirement | Install |
|---|---|
| Python 3.10+ | — |
| **osmium-tool** | `sudo apt install osmium-tool` / `brew install osmium-tool` |
| **duckOSM** | `pip install git+https://github.com/Khoshkhah/duckOSM.git` |
| **playwright + chromium** | `pip install playwright && playwright install chromium` |
| Mapbox token | [account.mapbox.com](https://account.mapbox.com/access-tokens/) |
| Google Maps JS API key | [console.cloud.google.com](https://console.cloud.google.com/) — enable **Maps JavaScript API** |
| TomTom API key | [developer.tomtom.com](https://developer.tomtom.com/) — Traffic Flow Tile API |
| MotherDuck token *(optional)* | [app.motherduck.com](https://app.motherduck.com/) — cloud sync via `scripts/motherduck_sync.py` |

---

## Installation

A dedicated conda environment is recommended — duckOSM is installed editable from a
sibling checkout so any fix landed there is picked up immediately.

```bash
git clone https://github.com/khoshkhah/osm-traffic-enrichment.git
git clone https://github.com/khoshkhah/duckOSM.git  ../duckOSM
cd osm-traffic-enrichment

conda create -n osm-traffic-enrichment python=3.11 -y
conda activate osm-traffic-enrichment

pip install -r requirements.txt
pip install -e ../duckOSM
playwright install chromium

cp .env.example .env
# Edit .env:
#   MAPBOX_ACCESS_TOKEN=pk.eyJ1...
#   GOOGLE_MAPS_API_KEY=AIza...
#   TOMTOM_API_KEY=...
#   MOTHERDUCK_TOKEN=...        (optional — enables auto-sync after each traffic run)
```

---

## Quick start

### Step 1 — Build the network (first time, no config needed)

For a new area there is no config file yet. Run the network pipeline with just the
place name — it auto-fetches the boundary from Nominatim, downloads the country PBF
from Geofabrik, builds the road network, and generates H3 boundary cells.
**The pipeline automatically creates `config/{name}.yaml` and `config/{name}_traffic.yaml`** during this step.

```bash
python pipeline_network.py --place "Tartu, Estonia" --name tartu
```

After this run you will have:
- `db/tartu.duckdb` — routable network with driving / walking / cycling schemas
- `boundaries/tartu.geojson` — boundary polygon
- `config/tartu.yaml` — auto-generated network config (edit to customise modes, H3 resolutions, etc.)
- `config/tartu_traffic.yaml` — auto-generated traffic config
- `output/tartu_{6,7,8}_h3_cells.geojson` — H3 boundary cells

### Step 2 — Fetch traffic (configs are already created!)

Since `config/tartu_traffic.yaml` was automatically created in Step 1, you can fetch traffic immediately:

```yaml
name: tartu
db_path: db/tartu.duckdb
refresh: false
traffic_sources: [mapbox, google, tomtom]
mapbox_zoom: 14
google_zoom: 16
tomtom_zoom: 14
```

Then fetch traffic:

```bash
python pipeline_traffic.py --config config/tartu_traffic.yaml
```

Or without a config file at all:

```bash
python pipeline_traffic.py --db db/tartu.duckdb --name tartu
```

### Subsequent network rebuilds — use the config

Once `config/tartu.yaml` exists you can pass it to control modes, H3 resolutions, etc.:

```bash
# Rebuild with updated options
python pipeline_network.py --config config/tartu.yaml --refresh
```

### Single source or forced refresh

```bash
# Mapbox only
python pipeline_traffic.py --config config/tartu_traffic.yaml --source mapbox

# Force network rebuild
python pipeline_network.py --config config/tartu.yaml --refresh

# Force tile cache clear + re-fetch traffic
python pipeline_traffic.py --config config/tartu_traffic.yaml --refresh
```

---

## Interactive Segment Selector Dashboard

A glassmorphic GIS dashboard for selecting road segments, scrubbing through historical
traffic, overlaying H3 hexagon grids, saving named edge selections back into the
database, and exporting them as CSV (with WKT geometries) or GeoJSON.

### Cloud version (recommended) — `docs/sensor_selector_cloud.html`

The current dashboard runs as a static page hosted on GitHub Pages. It talks directly
to MotherDuck from the browser via `@motherduck/wasm-client`, so no local server is needed.

[Open the dashboard ↗](https://khoshkhah.github.io/osm-traffic-enrichment/sensor_selector_cloud.html)

On first load it prompts for a [MotherDuck token](https://app.motherduck.com/settings),
which is stored in browser `localStorage` only on your device. Area timezones are read
from `main.visualization_metadata.timezone` so Run History timestamps render in local time.

### Local version — `scripts/sensor_selector.py` (legacy)

The older local-server variant — runs an `http.server` on port 8080 against a local
`.duckdb` file. Kept for offline / pre-MotherDuck workflows; not as up-to-date as the
cloud version (it still uses `timeapi.io` for timezone resolution).

```bash
python scripts/sensor_selector.py
```

### Key Features (both versions)
1. **Interactive Leaflet Map** — click any road segment to select/deselect.
2. **Road Styling Themes** — toggle road colors by OSM road category, slate gray, bright white, or live Mapbox / Google / TomTom traffic congestion.
3. **Historical Playback** — scrub through all timestamped runs in the `runs` table for the active source.
4. **H3 Cell Index Grid** — overlay boundary H3 cells with opacity controls and resolution selector.
5. **Saved Selections Manager** — name a selection, attach a free-text note, persist to `saved_selections` + `saved_selections_meta`, overlay or reload at any time.
6. **Exporters** — download active selections as GeoJSON or CSV (CSV uses `LINESTRING (...)` WKT for easy spreadsheet ingest).

---

## Config files

### Network config (`config/{name}.yaml`)

**Auto-generated by the network pipeline** the first time you run `pipeline_network.py` for an area.
You can then edit it to add pipeline-specific keys (`h3_resolutions`, `country_url`, etc.)
or copy `config/network.template.yaml` as a starting point for a new area.

```yaml
name: myarea
boundary_path: boundaries/myarea.geojson
country_url: https://download.geofabrik.de/europe/country-latest.osm.pbf
refresh: false

h3_resolutions: [6, 7, 8]   # h3_resolution for edge indexing = max(this list)

output_path: db
options:
  build_graph: true
  h3_indexing: true
  simplify: true
  process_speeds: true
  extract_restrictions: true
  calculate_costs: true
modes:
  - driving
  - walking
  - cycling
```

### Traffic config (`config/{name}_traffic.yaml`)

**Auto-generated by the network pipeline** the first time you run `pipeline_network.py` for an area.
Controls traffic fetching — you can edit it to adjust zoom levels or select specific sources.
The boundary polygon is read from the DuckDB `boundary` table — no path needed.

```yaml
name: myarea
db_path: db/myarea.duckdb
refresh: false

traffic_sources: [mapbox, google, tomtom]
mapbox_zoom: 14    # MVT tile zoom
google_zoom: 16    # Playwright screenshot zoom (~2.4 m/px)
tomtom_zoom: 14    # TomTom PBF tile zoom
```

---

## Database schema

Each source writes to its own history table. `driving.edges` contains pure OSM data — no traffic columns.

| Table | Schema | Description |
|---|---|---|
| `edges` | `driving` / `walking` / `cycling` | Road segments — geometry, speed, cost, H3 index |
| `runs` | `main` | One row per traffic fetch execution — includes `n_tiles`, effective `zoom` |
| `mapbox_congestion_history` | `main` | Mapbox congestion per edge per run |
| `google_congestion_history` | `main` | Google congestion per edge per run |
| `tomtom_congestion_history` | `main` | TomTom congestion + raw `traffic_level` per edge per run |
| `boundary_cells` | `main` | H3 hexagon grid at multiple resolutions |
| `boundary` | `main` | Boundary polygon |
| `visualization_metadata` | `main` | Per-area centroid, initial zoom, IANA timezone |
| `saved_selections` + `saved_selections_meta` | `main` | Edge selections saved through the Sensor Selector webpage |

**Congestion values:** `low` · `moderate` · `heavy` · `severe` · `no data`

See **[docs/database_schema.md](docs/database_schema.md)** for the full column listing.

---

## `traffic_db` library

`scripts/traffic_db.py` provides a clean Python API for querying the DuckDB:

```python
import sys; sys.path.insert(0, '..')
from scripts.traffic_db import TrafficDB

with TrafficDB('db/tartu.duckdb') as db:
    # Latest congestion per source
    edges   = db.get_edges(source='mapbox', congestion='heavy')
    summary = db.get_congestion_summary(source='google')

    # Side-by-side map
    db.plot_comparison(sources=['mapbox', 'google', 'tomtom'])

    # History
    db.get_congestion_history(road_name='Pikk', source='mapbox')

    # All history runs
    db.get_history_index()
```

See **[docs/traffic_db.md](docs/traffic_db.md)** for the full API reference.

---

## Documentation

| File | Contents |
|---|---|
| [docs/pipeline_cli.md](docs/pipeline_cli.md) | Full CLI reference for both pipelines |
| [docs/traffic_data_flow.md](docs/traffic_data_flow.md) | End-to-end: what each source returns, how it's processed, what gets stored |
| [docs/matching.md](docs/matching.md) | Map-matching: route-based (default) vs geometric, aggregation & coverage rules |
| [docs/database_schema.md](docs/database_schema.md) | DuckDB tables and columns |
| [docs/traffic_db.md](docs/traffic_db.md) | `scripts/traffic_db.py` API |
| [docs/traffic_status.md](docs/traffic_status.md) | Congestion level definitions |

---

## Related

- [duckOSM](https://github.com/Khoshkhah/duckOSM) — OSM → DuckDB network builder
- [Mapbox Traffic v1](https://docs.mapbox.com/data/tilesets/reference/mapbox-traffic-v1/)
- [TomTom Traffic Flow Tiles](https://developer.tomtom.com/traffic-api/documentation/traffic-flow/vector-flow-tiles)
- [googletraffic R package](https://github.com/dime-worldbank/googletraffic) — inspiration for the Playwright approach and reference colors
- [Geofabrik OSM extracts](https://download.geofabrik.de/)

---

## License

MIT — see [LICENSE](LICENSE).
