# osm-traffic-enrichment

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
├── pipeline_network.py              # Build network + H3 cells
├── pipeline_traffic.py              # Fetch traffic from all sources
├── pipeline.py                      # Combined wrapper (runs both)
│
├── scripts/
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
│   ├── sodermalm.yaml               # Network config — Södermalm, Stockholm
│   ├── sodermalm_traffic.yaml       # Traffic config — Södermalm
│   ├── tartu.yaml                   # Network config — Tartu, Estonia
│   └── tartu_traffic.yaml           # Traffic config — Tartu
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
│   └── traffic_status.md            # Congestion level definitions
│
└── boundaries/
    ├── sodermalm.geojson
    ├── nacka.geojson
    └── tartu.geojson
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

---

## Installation

```bash
git clone https://github.com/khoshkhah/osm-traffic-enrichment.git
cd osm-traffic-enrichment

pip install -r requirements.txt
playwright install chromium

cp .env.example .env
# Edit .env:
#   MAPBOX_ACCESS_TOKEN=pk.eyJ1...
#   GOOGLE_MAPS_API_KEY=AIza...
#   TOMTOM_API_KEY=...
```

---

## Quick start

### Using a config file (recommended)

```bash
# 1. Build the network once
python pipeline_network.py --config config/tartu.yaml

# 2. Fetch traffic regularly
python pipeline_traffic.py --config config/tartu_traffic.yaml
```

### Without a config file

```bash
# Auto-fetch boundary + detect country PBF, build all modes + H3 cells
python pipeline_network.py --place "Tartu, Estonia" --name tartu

# Fetch traffic for a specific DuckDB
python pipeline_traffic.py --db db/tartu.duckdb --name tartu
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

See **[docs/pipeline_cli.md](docs/pipeline_cli.md)** for the full argument reference.

---

## Config files

### Network config (`config/{name}.yaml`)

Controls network building — copy `config/network.template.yaml` to get started.

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

Controls traffic fetching — copy `config/traffic.template.yaml` to get started.
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
| `runs` | `main` | One row per traffic fetch execution |
| `mapbox_congestion_history` | `main` | Mapbox congestion per edge per run |
| `google_congestion_history` | `main` | Google congestion per edge per run |
| `tomtom_congestion_history` | `main` | TomTom congestion + raw `traffic_level` per edge per run |
| `boundary_cells` | `main` | H3 hexagon grid at multiple resolutions |
| `boundary` | `main` | Boundary polygon |

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
