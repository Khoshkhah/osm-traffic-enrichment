# Pipeline CLI Reference

Two separate pipelines handle different responsibilities:

| Pipeline | Purpose | Run frequency |
|---|---|---|
| `pipeline_network.py` | Build OSM road network + H3 boundary cells | Once per area (or on rebuild) |
| `pipeline_traffic.py` | Fetch traffic + write to history tables | Regularly (hourly / daily) |

Chain them for a full build from scratch:

```bash
python pipeline_network.py --config config/tartu.yaml && \
python pipeline_traffic.py --config config/tartu_traffic.yaml
```

---

## `pipeline_network.py`

```
python pipeline_network.py [OPTIONS]

Boundary (one required):
  --place PLACE_NAME         Auto-fetch boundary from Nominatim + detect country PBF
  --boundary FILE            Path to an existing boundary GeoJSON

PBF source (optional when --place is given):
  --pbf FILE                 Local country .osm.pbf file
  --country-url URL          Geofabrik download URL

Required (unless set in config):
  --name NAME                Area name used for all output filenames

Optional:
  --config FILE              Network config YAML (see config/network.template.yaml)
  --modes MODE [MODE ...]    Routing modes, e.g. --modes driving walking cycling
                             (default: driving walking cycling)
  --h3-resolutions RES [...] H3 resolutions for boundary_cells, e.g. --h3-resolutions 6 7 8
                             (default: 6 7 8)
  --no-h3                    Skip H3 boundary cell generation
  --refresh                  Delete cached PBF + DuckDB before running
```

### Config file keys (`config/network.template.yaml`)

All CLI flags have a config equivalent — CLI always overrides config:

| Config key | CLI flag | Default |
|---|---|---|
| `name` | `--name` | required |
| `place` | `--place` | — |
| `boundary_path` | `--boundary` | — |
| `country_url` | `--country-url` | — |
| `country_pbf` | `--pbf` | — |
| `modes` | `--modes` | `[driving, walking, cycling]` |
| `h3_resolutions` | `--h3-resolutions` | `[6, 7, 8]` |
| `refresh` | `--refresh` | `false` |
| `output_path` | — | `db` |
| `options` | — | duckOSM build options |

`h3_resolution` for edge indexing is derived automatically from `max(h3_resolutions)`.
You do not need to set it in `options`.

### What it builds

| Step | Output |
|---|---|
| Fetch boundary | `boundaries/{name}.geojson` |
| Download country PBF | `map/{country}-latest.osm.pbf` |
| Filter PBF | `pbf/{name}.osm.pbf` |
| Build network | `db/{name}.duckdb` with `driving/walking/cycling` schemas |
| H3 boundary cells | `boundary_cells` table in DuckDB + CSV/GeoJSON in `output/` |

### Examples

```bash
# Config file (recommended)
python pipeline_network.py --config config/tartu.yaml

# Place name — auto-detects everything
python pipeline_network.py --place "Tartu, Estonia" --name tartu

# Force rebuild
python pipeline_network.py --config config/tartu.yaml --refresh

# Existing boundary + local PBF, driving only, no H3
python pipeline_network.py \
  --name tartu \
  --boundary boundaries/tartu.geojson \
  --pbf map/estonia-latest.osm.pbf \
  --modes driving \
  --no-h3

# Custom H3 resolutions
python pipeline_network.py --place "Riga, Latvia" --name riga \
  --h3-resolutions 7 8 9
```

---

## `pipeline_traffic.py`

```
python pipeline_traffic.py [OPTIONS]

Required (unless set in config):
  --name NAME                Area name (for logs and tile cache dirs)
  --db FILE                  Path to DuckDB built by pipeline_network.py

Optional:
  --config FILE              Traffic config YAML (see config/traffic.template.yaml)
  --source SOURCE            mapbox | google | tomtom | all
                             (default: all sources listed in config)
  --mapbox-zoom INT          Mapbox MVT tile zoom  (default: config value or 14)
  --google-zoom INT          Google Maps screenshot zoom  (default: config value or 16)
  --tomtom-zoom INT          TomTom PBF tile zoom  (default: config value or 14)
  --refresh                  Delete cached tile files before running
                             (does NOT touch the DuckDB)
```

### Config file keys (`config/traffic.template.yaml`)

| Config key | CLI flag | Default |
|---|---|---|
| `name` | `--name` | required |
| `db_path` | `--db` | `db/{name}.duckdb` |
| `traffic_sources` | `--source all` | `[mapbox, google, tomtom]` |
| `mapbox_zoom` | `--mapbox-zoom` | `14` |
| `google_zoom` | `--google-zoom` | `16` |
| `tomtom_zoom` | `--tomtom-zoom` | `14` |
| `refresh` | `--refresh` | `false` |

The boundary polygon is read from the DuckDB `boundary` table — no `boundary_path` needed.

### Required API keys (`.env`)

| Key | Source | Required for |
|---|---|---|
| `MAPBOX_ACCESS_TOKEN` | [account.mapbox.com](https://account.mapbox.com/access-tokens/) | `mapbox` source |
| `GOOGLE_MAPS_API_KEY` | Google Cloud Console — **Maps JavaScript API** | `google` source |
| `TOMTOM_API_KEY` | [developer.tomtom.com](https://developer.tomtom.com/) | `tomtom` source |

### What it writes

Each source appends to its own history table. `driving.edges` is never modified.

| Source | History table | Extra columns |
|---|---|---|
| Mapbox | `mapbox_congestion_history` | — |
| Google | `google_congestion_history` | — |
| TomTom | `tomtom_congestion_history` | `traffic_level DOUBLE` (raw 0.0–1.0) |

Also exports `output/{name}_edges_traffic_{source}.geojson` and `.csv`.

### Examples

```bash
# Config file (recommended)
python pipeline_traffic.py --config config/tartu_traffic.yaml

# Single source
python pipeline_traffic.py --config config/tartu_traffic.yaml --source mapbox

# Without config
python pipeline_traffic.py --db db/tartu.duckdb --name tartu

# Force tile cache refresh
python pipeline_traffic.py --config config/tartu_traffic.yaml --refresh

# Custom zoom for one source
python pipeline_traffic.py --config config/tartu_traffic.yaml \
  --source google --google-zoom 15
```

---

## Zoom levels

### Mapbox (`--mapbox-zoom`, default 14)

| Zoom | Tile coverage | Road classes |
|---|---|---|
| 12 | ~4 km/tile | motorway, trunk, primary, secondary |
| 13 | ~2 km/tile | + tertiary |
| 14 | ~1 km/tile | + residential *(recommended)* |
| 15 | ~500 m/tile | + service roads |

### Google Maps (`--google-zoom`, default 16)

| Zoom | Resolution | Notes |
|---|---|---|
| 14 | ~9.5 m/px | Fast, less detail |
| 15 | ~4.8 m/px | — |
| 16 | ~2.4 m/px | Traffic stripes ~3 px wide *(recommended)* |
| 17 | ~1.2 m/px | Very large screenshots — auto-reduced if > 160 MP |

### TomTom (`--tomtom-zoom`, default 14)

Same tile grid as Mapbox. Use zoom 14 to match Mapbox tiles for direct comparison.

---

## Caching

Each step checks whether its output already exists and skips if so.

| Pipeline | Step | Cached when |
|---|---|---|
| network | Fetch boundary | `boundaries/{name}.geojson` exists |
| network | Download PBF | `map/{country}-latest.osm.pbf` exists |
| network | Filter PBF | `pbf/{name}.osm.pbf` exists |
| network | Build network | `db/{name}.duckdb` exists |
| network | H3 cells | existing rows replaced per resolution (never fully skipped) |
| traffic | Mapbox tile fetch | `output/{name}_traffic_mapbox.geojson` exists |
| traffic | TomTom tile fetch | `output/{name}_traffic_tomtom.geojson` exists |
| traffic | Google render | never cached (screenshot is always fresh) |
| traffic | Map match + write | never skipped — always appends to history |

Use `--refresh` to clear the relevant cache:
- `pipeline_network.py --refresh` deletes `pbf/{name}.osm.pbf` + `db/{name}.duckdb`
- `pipeline_traffic.py --refresh` deletes cached tile files + traffic GeoJSONs (keeps DuckDB)

---

## Log files

Every run writes to `logs/pipeline_{name}_{timestamp}.log`.

- **Console** (INFO): clean one-line-per-event progress
- **Log file** (DEBUG): full detail — tile counts, HTTP status, distributions, tracebacks

Summary printed at the end of every run:

```
══════════════════════════════════════════════════════════════
  PIPELINE SUMMARY — tartu
══════════════════════════════════════════════════════════════
  Step                                 Status       Time
  ──────────────────────────────────────────────────────────
  Refresh — delete cached files        done          0.0s
  Download country PBF                 done          0.0s
  Filter PBF by boundary               done          1.9s
  Build road network (duckOSM)         done          2.4s
  Generate H3 boundary cells           done          0.1s
  ──────────────────────────────────────────────────────────
  TOTAL                                              4.4s
══════════════════════════════════════════════════════════════
```
