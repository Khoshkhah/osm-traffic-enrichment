# Pipeline CLI Reference

`pipeline.py` is the end-to-end command-line interface that runs all four enrichment
steps in sequence: filter PBF → build network → fetch traffic → map match.

---

## Quick reference

```
python pipeline.py [OPTIONS]

Boundary (one required):
  --place PLACE_NAME       Fetch boundary automatically by place name
  --boundary FILE          Path to an existing boundary GeoJSON

PBF source (optional when --place is given):
  --pbf FILE               Path to a local .osm.pbf file
  --country-url URL        Geofabrik download URL

Required:
  --name NAME              Area name used for all output filenames

Optional:
  --zoom INT               Mapbox tile zoom level  [default: 14]
                           Zoom 14 is sufficient for Mapbox — tiles cover ~2.4 km²
                           each and already contain road-level segment data.
  --google-zoom INT        Google Maps screenshot zoom level  [default: 16]
                           Zoom 16 gives ~2.4 m/px so traffic stripes are 3-4 px wide.
                           For very large areas the pipeline auto-reduces zoom to stay
                           within PIL's 160 MP image-size limit.
  --traffic-source SOURCE  mapbox | google | both  [default: both]
  --config FILE            duckOSM YAML config path
  --refresh                Delete cached files for this area before running
```

### Required environment variables (`.env`)

| Variable | Source | Required for |
|---|---|---|
| `MAPBOX_ACCESS_TOKEN` | [account.mapbox.com](https://account.mapbox.com/access-tokens/) | `--traffic-source mapbox\|both` |
| `GOOGLE_MAPS_API_KEY` | Google Cloud Console — **Maps JavaScript API** | `--traffic-source google\|both` |

---

## Arguments

### `--place PLACE_NAME`

A human-readable place name that will be passed to the **OpenStreetMap Nominatim API**
to fetch the administrative boundary polygon automatically.

- Saves the boundary to `boundaries/{name}.geojson`
- Cached: if the file already exists, Nominatim is still queried (to get the country
  code for auto-detecting the PBF URL) but the file is not overwritten
- Also detects the **country code** (e.g. `ee`, `se`) and resolves the Geofabrik
  download URL automatically — so `--country-url` and `--pbf` are optional

**Examples:**
```bash
--place "Tartu, Estonia"
--place "Nacka, Sweden"
--place "Berlin, Germany"
--place "Osaka, Japan"
```

**Tip:** Add the country name to avoid ambiguity — `"Springfield"` alone could match
dozens of places.

---

### `--boundary FILE`

Path to an existing boundary GeoJSON file. Use this when you already have the boundary
(e.g. created by `notebook/0_get_boundary.ipynb`).

Mutually exclusive with `--place`.

**Example:**
```bash
--boundary boundaries/tartu.geojson
```

---

### `--name NAME`  *(required)*

The area name used as a prefix for **all output filenames**:

| File | Path |
|---|---|
| Filtered PBF | `pbf/{name}.osm.pbf` |
| DuckDB | `db/{name}.duckdb` |
| Traffic GeoJSON | `output/{name}_traffic.geojson` |
| Enriched GeoJSON | `output/{name}_edges_traffic.geojson` |
| Enriched CSV | `output/{name}_edges_traffic.csv` |
| Log file | `logs/pipeline_{name}_{timestamp}.log` |

Use lowercase, no spaces (use underscores if needed): `tartu`, `nacka`, `new_york`.

---

### `--pbf FILE`

Path to a local `.osm.pbf` file. Use this when the country PBF is already on disk
(avoids re-downloading hundreds of MB).

Mutually exclusive with `--country-url`.

**Example:**
```bash
--pbf map/sweden-latest.osm.pbf
```

**Tip:** Country PBFs are cached in `map/` — once downloaded for one area they are
reused for all other areas in the same country.

---

### `--country-url URL`

Full Geofabrik download URL for the country PBF. The file is downloaded to `map/`
and cached — subsequent runs skip the download.

Mutually exclusive with `--pbf`.

**When to use:** when `--place` is not given (you have a manual `--boundary`) and the
PBF is not yet downloaded.

**URL format:** `https://download.geofabrik.de/{continent}/{country}-latest.osm.pbf`

**Examples:**
```bash
--country-url https://download.geofabrik.de/europe/estonia-latest.osm.pbf
--country-url https://download.geofabrik.de/europe/sweden-latest.osm.pbf
--country-url https://download.geofabrik.de/north-america/us/california-latest.osm.pbf
```

Browse all available files at **https://download.geofabrik.de/**

**When `--place` is given**, the country URL is resolved automatically from the
Nominatim result — you do not need this flag.

---

### `--zoom INT`

Mapbox tile zoom level for fetching traffic data. Default: `14`.

| Zoom | Tile coverage | Road detail |
|---|---|---|
| 12 | ~4 km per tile | motorway, trunk, primary, secondary |
| 13 | ~2 km per tile | + tertiary |
| 14 | ~1 km per tile | + residential, street *(recommended)* |
| 15 | ~500 m per tile | + service roads |

Higher zoom = more tiles to download, more road classes covered, slower fetch step.

---

### `--config FILE`

Path to a duckOSM YAML configuration file. Controls which transportation modes are
built, H3 resolution, simplification, etc.

If not provided, the pipeline looks for `config/{name}.yaml` first (generated by
`notebook/1_filter_pbf.ipynb`), then falls back to programmatic defaults:
- Modes: `driving` only
- H3 resolution: 8
- `build_graph`, `h3_indexing`, `simplify`, `process_speeds`, `extract_restrictions`,
  `calculate_costs` all enabled

**Example config (`config/tartu.yaml`):**
```yaml
name: "tartu"
pbf_path: "/path/to/pbf/tartu.osm.pbf"
output_path: "/path/to/db"
boundary_path: "/path/to/boundaries/tartu.geojson"
options:
  build_graph: true
  h3_indexing: true
  h3_resolution: 8
  simplify: true
  process_speeds: true
  extract_restrictions: true
  calculate_costs: true
modes:
  - driving
  - walking
  - cycling
```

---

### `--refresh`

Deletes all **area-specific** cached files before running, forcing a complete re-run.

**Deleted** (for the given `--name`):
- `pbf/{name}.osm.pbf`
- `db/{name}.duckdb` and `db/{name}.duckdb.wal`
- `output/{name}_traffic.geojson` and `.csv`
- `output/{name}_edges_traffic.geojson` and `.csv`

**Preserved** (shared resources):
- `map/*.osm.pbf` — country PBFs used by multiple areas
- `tiles/**/*.mvt` — cached Mapbox tiles (identified by z/x/y, not area name)
- `boundaries/` — input boundary files

**Use cases:**
- Re-run after updating the boundary polygon
- Rebuild the network with different duckOSM options
- Fetch fresh traffic data (different time of day)

---

## Usage patterns

### Simplest — everything auto-detected
```bash
python pipeline.py --place "Tartu, Estonia" --name tartu
```
Fetches boundary from Nominatim, detects Estonia → downloads `estonia-latest.osm.pbf`,
runs all four steps.

---

### Second run — boundary and PBF cached, fresh traffic
```bash
# Delete only the traffic cache, keep the network
rm output/tartu_traffic.geojson
python pipeline.py --place "Tartu, Estonia" --name tartu
```

---

### Force complete re-run
```bash
python pipeline.py --place "Tartu, Estonia" --name tartu --refresh
```

---

### Reuse existing PBF (already downloaded)
```bash
python pipeline.py \
  --place "Nacka, Sweden" \
  --pbf map/sweden-latest.osm.pbf \
  --name nacka \
  --zoom 14
```

---

### Fully manual (all paths explicit)
```bash
python pipeline.py \
  --boundary  boundaries/tartu.geojson \
  --pbf       map/estonia-latest.osm.pbf \
  --name      tartu \
  --zoom      14 \
  --config    config/tartu.yaml
```

---

## Pipeline steps and caching

Each step checks whether its output file already exists and skips if so.
The log and summary show `skipped` for cached steps and their time as `0.0s`.

| Step | Trigger | Output file | Cached when |
|---|---|---|---|
| Fetch boundary | `--place` | `boundaries/{name}.geojson` | file exists |
| Download PBF | `--country-url` or auto | `map/{country}-latest.osm.pbf` | file exists |
| Filter PBF | always | `pbf/{name}.osm.pbf` | file exists |
| Build network | always | `db/{name}.duckdb` | file exists |
| Fetch traffic | always | `output/{name}_traffic.geojson` | file exists |
| Map match | always | updates `db/{name}.duckdb` | **never skipped** |

Map match always runs because it applies the latest traffic data to the network.
To skip it, the traffic GeoJSON must not have changed since the last run.

---

## Log file

Every run writes a log to `logs/pipeline_{name}_{timestamp}.log`.

**Console output** (INFO level) — clean progress lines, one per event.

**Log file** (DEBUG level) — full detail: tile counts, HTTP status, file sizes,
congestion distributions, tracebacks on errors.

**Summary at end of log:**
```
══════════════════════════════════════════════════════════════
  PIPELINE SUMMARY — tartu
══════════════════════════════════════════════════════════════
  Step                                 Status       Time
  ──────────────────────────────────────────────────────────
  Fetch boundary from place name       skipped       0.0s
  Download country PBF                 skipped       0.0s
  Filter PBF by boundary               done          4.1s
  Build road network (duckOSM)         done         61.3s
  Fetch Mapbox traffic tiles           done         12.8s
  Map match + enrich DuckDB            done          9.2s
  ──────────────────────────────────────────────────────────
  TOTAL                                             87.4s
══════════════════════════════════════════════════════════════
```
