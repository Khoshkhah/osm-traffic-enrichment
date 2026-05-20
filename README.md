# osm-traffic-enrichment

An end-to-end pipeline that enriches an OSM road network with real-time Mapbox traffic
congestion, producing a routable **DuckDB** database where every edge has a `congestion` column.

This project combines three tools:

| Tool | Source | Role |
|---|---|---|
| **osmium-tool** | system package | Clips a large OSM PBF to your boundary |
| **duckOSM** | [h3-routing-platform](https://github.com/khoshkhah/h3-routing-platform) | Builds a routable DuckDB network from the PBF |
| **Mapbox Traffic v1** | Mapbox API | Provides real-time congestion per road segment |

---

## Pipeline overview

```
boundary.geojson  +  region.osm.pbf  +  Mapbox token
           │
           ▼
  1️⃣  Filter PBF by boundary         (osmium extract)
           │
           ▼  pbf/{name}.osm.pbf
           │
  2️⃣  Build road network             (duckOSM)
           │
           ▼  db/{name}.duckdb
           │    └─ driving.edges  (edge_id, highway, name, oneway,
           │                       length_m, cost_s, h3_cell, geometry, …)
           │
  3️⃣  Fetch Mapbox traffic tiles     (Traffic v1 + Streets v8)
           │
           ▼  output/{name}_traffic.geojson
           │
  4️⃣  Map match + enrich             (one-to-many geometric matching)
           │
           ▼  db/{name}.duckdb          ← driving.edges gains congestion column
              output/{name}_edges_traffic.geojson
              output/{name}_edges_traffic.csv
```

### Map matching algorithm

A single Mapbox traffic segment typically covers **many** OSM edges.
We use a **one-to-many geometric matching** strategy:

```
For each Mapbox segment T (with real congestion):
  1. Draw a 25 m corridor around T
  2. Use R-tree index to get candidate OSM edges quickly
  3. Reject edges whose bearing differs by > 45° (parallel/opposite roads)
  4. Reject edges where < 40% of their length lies inside the corridor
  5. Assign T.congestion to all remaining edges
  6. If an edge is claimed by multiple segments → keep most severe
```

Typical result: **> 98% of edges** receive a congestion value.

---

## Project structure

```
osm-traffic-enrichment/
├── pipeline.py                     # CLI: run all 4 steps end-to-end
├── notebook/
│   ├── 1_filter_pbf.ipynb          # Step 1: osmium extract
│   ├── 2_build_network.ipynb       # Step 2: duckOSM → DuckDB + stats
│   ├── 3_fetch_traffic.ipynb       # Step 3: Mapbox tiles → GeoJSON
│   └── 4_map_match.ipynb           # Step 4: match + write DuckDB + stats
├── scripts/
│   └── filter_pbf.py               # Utility: osmium extract wrapper
├── config/
│   ├── sodermalm.yaml              # duckOSM config for Södermalm
│   └── nacka.yaml                  # duckOSM config for Nacka
├── boundaries/
│   ├── sodermalm.geojson           # Sample boundary (Södermalm, Stockholm)
│   └── nacka.geojson               # Sample boundary (Nacka, Stockholm)
└── data/
    └── sodermalm_edges.csv         # Sample output (no PBF needed to inspect)
```

**Generated at runtime (git-ignored):**
```
pbf/         ← filtered OSM PBF files
db/          ← DuckDB databases
tiles/       ← cached Mapbox .mvt tiles
output/      ← GeoJSON + CSV exports
```

---

## Prerequisites

| Requirement | Install |
|---|---|
| Python 3.10+ | — |
| **osmium-tool** | `sudo apt install osmium-tool` / `brew install osmium-tool` |
| **duckOSM** | `pip install -e ../h3-routing-platform/tools/duckOSM` |
| Mapbox account | [account.mapbox.com](https://account.mapbox.com/access-tokens/) |
| OSM PBF file | [download.geofabrik.de](https://download.geofabrik.de/) |

---

## Installation

```bash
git clone https://github.com/khoshkhah/osm-traffic-enrichment.git
cd osm-traffic-enrichment

pip install -r requirements.txt

# Install duckOSM from the h3-routing-platform repo
pip install -e ../h3-routing-platform/tools/duckOSM

cp .env.example .env
# Edit .env and add your Mapbox token
```

---

## Quick start — CLI

```bash
python pipeline.py \
  --pbf      /path/to/sweden-latest.osm.pbf \
  --boundary boundaries/sodermalm.geojson \
  --name     sodermalm \
  --zoom     14
```

The pipeline is **resumable** — already-completed steps are detected by the presence of
their output file and skipped automatically.

---

## Quick start — Notebooks

Run the four notebooks in order inside JupyterLab:

```bash
jupyter lab
```

| Notebook | Input | Output |
|---|---|---|
| `1_filter_pbf.ipynb` | `region.osm.pbf` + boundary | `pbf/{name}.osm.pbf` |
| `2_build_network.ipynb` | filtered PBF | `db/{name}.duckdb` |
| `3_fetch_traffic.ipynb` | boundary + Mapbox token | `output/{name}_traffic.geojson` |
| `4_map_match.ipynb` | DuckDB + traffic GeoJSON | enriched DuckDB + CSV/GeoJSON |

Edit the **Configuration** cell at the top of each notebook to set `NAME` and file paths.

---

## Output format

### DuckDB (`db/{name}.duckdb`)

The `driving.edges` table gains a `congestion` column:

| Column | Type | Description |
|---|---|---|
| `edge_id` | INTEGER | Unique edge ID |
| `highway` | VARCHAR | OSM road type |
| `name` | VARCHAR | Street name |
| `oneway` | VARCHAR | Directionality |
| `length_m` | FLOAT | Length in metres |
| `cost_s` | FLOAT | Travel time in seconds |
| `from_cell` | BIGINT | H3 source cell |
| `to_cell` | BIGINT | H3 target cell |
| `geometry` | GEOMETRY | LineString (WGS-84) |
| **`congestion`** | **VARCHAR** | **`low` / `moderate` / `heavy` / `severe` / `no data`** |

Query example:
```sql
-- Congestion summary
SELECT congestion, count(*) AS edges, round(sum(length_m)/1000, 1) AS km
FROM driving.edges
GROUP BY congestion
ORDER BY km DESC;
```

### CSV (`output/{name}_edges_traffic.csv`)

Same columns as above plus `lon`, `lat` (centroid), `start_lon/lat`, `end_lon/lat`.

---

## Configuration

duckOSM processing options are set in `config/{name}.yaml`:

```yaml
name: sodermalm
pbf_path: ../pbf/sodermalm.osm.pbf
output_path: ../db

options:
  build_graph: true        # edge adjacency table for routing
  h3_indexing: true        # H3 spatial index
  h3_resolution: 8         # H3 resolution (0-15)
  simplify: true           # contract degree-2 nodes
  process_speeds: true
  extract_restrictions: true
  calculate_costs: true

modes:
  - driving
  - walking
  - cycling
```

---

## Related

- [h3-routing-platform / duckOSM](https://github.com/khoshkhah/h3-routing-platform) — OSM → DuckDB network builder used in step 2
- [Mapbox Traffic v1 tileset](https://docs.mapbox.com/data/tilesets/reference/mapbox-traffic-v1/)
- [Mapbox Vector Tile specification](https://github.com/mapbox/vector-tile-spec)
- [Geofabrik OSM extracts](https://download.geofabrik.de/)

---

## License

MIT — see [LICENSE](LICENSE).
