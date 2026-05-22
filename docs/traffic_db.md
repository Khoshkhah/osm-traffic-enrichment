# `traffic_db` — Python Library Reference

`scripts/traffic_db.py` is a lightweight Python library for reading and writing the
osm-traffic-enrichment DuckDB database. It wraps common queries into a clean API
and provides Folium visualizations.

Each traffic source has its own history table:
- `mapbox_congestion_history`
- `google_congestion_history`
- `tomtom_congestion_history` (also stores the raw `traffic_level` float)

---

## Quick start

```python
import sys
sys.path.insert(0, '..')        # from a notebook in notebook/
from scripts.traffic_db import TrafficDB

# Read-only (default) — always returns latest run per source
with TrafficDB('db/sodermalm.duckdb') as db:
    print(db.get_history_index())

    edges   = db.get_edges(source='mapbox', congestion='heavy')
    summary = db.get_congestion_summary(source='mapbox')
    m       = db.plot_edges(edges)
```

```python
# Write congestion results (e.g. from notebook 3a)
with TrafficDB('db/sodermalm.duckdb', read_only=False) as db:
    run_id = db.write_congestion(
        edge_congestion,          # dict: edge_id → 'low'|'moderate'|'heavy'|'severe'
        source='mapbox',
        zoom=14,
        n_segments=total_segments,
        boundary_name='sodermalm',
    )

# Write TomTom results (includes raw traffic_level)
with TrafficDB('db/sodermalm.duckdb', read_only=False) as db:
    run_id = db.write_congestion(
        edge_congestion,
        source='tomtom',
        zoom=14,
        n_segments=len(gdf),
        boundary_name='sodermalm',
        traffic_levels=traffic_levels,   # dict: edge_id → float
    )
```

---

## Module constants

```python
from scripts.traffic_db import CONGESTION_COLORS, CONGESTION_ORDER
```

| Name | Type | Value |
|---|---|---|
| `CONGESTION_COLORS` | `dict[str, str]` | Hex color per level (matches Google Maps JS API) |
| `CONGESTION_ORDER` | `list[str]` | `['low', 'moderate', 'heavy', 'severe', 'no data']` |

Color mapping:

| Level | Hex | Color |
|---|---|---|
| `low` | `#11D68F` | Teal-green |
| `moderate` | `#FFCF43` | Yellow |
| `heavy` | `#F24E42` | Red |
| `severe` | `#A92727` | Dark red |
| `no data` | `#cccccc` | Gray |

---

## `TrafficDB` class

```python
TrafficDB(db_path, read_only=True)
```

### Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `db_path` | `str \| Path` | — | Path to the `.duckdb` file |
| `read_only` | `bool` | `True` | `False` required for write operations |

### Connection management

```python
# Context manager (recommended)
with TrafficDB('db/sodermalm.duckdb') as db:
    ...

# Manual close
db = TrafficDB('db/sodermalm.duckdb')
edges = db.get_edges()
db.close()
```

The `con` property returns the underlying `duckdb.DuckDBPyConnection` for raw SQL.

---

## Schema methods

### `list_tables() → DataFrame`

All tables in the database with schema and column count.

### `migrate_schema(source=None)`

Ensures the `runs` table and the `{source}_congestion_history` table exist.
Migrates data from the legacy `edge_congestion_history` table if present.
Requires `read_only=False`. Called automatically by `write_congestion()`.

---

## History queries

### `get_history_index() → DataFrame`

All historical runs — which sources have data, on what dates, and how many edges.

```python
db.get_history_index()
# run_id  source  boundary_name  zoom  fetched_at            edges_with_data  n_segments
#      1  mapbox  sodermalm        14  2026-05-22 01:57:06             4465        3554
#      2  google  sodermalm        16  2026-05-22 01:57:26              442      106405
```

### `get_runs() → DataFrame`

Alias for `get_history_index()`.

---

## Edge queries

### `get_edges(...) → GeoDataFrame`

```python
get_edges(
    mode='driving',      # 'driving' | 'walking' | 'cycling'
    source='mapbox',     # 'mapbox' | 'google' | 'tomtom'
    highway=None,        # filter by highway type
    congestion=None,     # filter by level
    name=None,           # partial road name search
    limit=None,
    run_id=None,         # None = latest run for source
)
```

Returns a GeoDataFrame. The `congestion` column contains the value from
`{source}_congestion_history`. A `congestion_{source}` column is added as a convenient alias.

### `get_network_stats(mode='driving') → DataFrame`

Edge count, total km, and average speed/cost per highway type.

### `get_nearby_edges(lon, lat, radius_m=500, mode='driving', source='mapbox') → GeoDataFrame`

Edges within `radius_m` metres of a point, with congestion from history.

---

## Congestion queries

### `get_congestion_summary(source='mapbox', mode='driving', run_id=None) → DataFrame`

Edge count, total km, and percentage per congestion level.

### `get_congestion_history(edge_id=None, road_name=None, source=None, limit=500) → DataFrame`

Time-series congestion records. **`source` is now required.**

```python
# Mapbox history for Ringvägen
db.get_congestion_history(road_name='Ringvägen', source='mapbox')

# TomTom history for a specific edge (includes traffic_level column)
db.get_congestion_history(edge_id=175, source='tomtom')
```

### `get_congestion_comparison(mode='driving', sources=None, run_ids=None) → GeoDataFrame`

All edges with congestion from multiple sources as side-by-side columns.

```python
# Default: mapbox + google
both = db.get_congestion_comparison()

# All three sources
all3 = db.get_congestion_comparison(sources=['mapbox', 'google', 'tomtom'])
```

---

## Write operations

All write methods require `read_only=False`.

### `write_congestion(edge_congestion, source, zoom, n_segments, boundary_name='', traffic_levels=None) → int`

Write congestion results to `{source}_congestion_history` and `runs`.

| Parameter | Type | Description |
|---|---|---|
| `edge_congestion` | `dict` | edge_id → `'low'`\|`'moderate'`\|`'heavy'`\|`'severe'` |
| `source` | `str` | `'mapbox'`, `'google'`, `'tomtom'`, or any string |
| `zoom` | `int` | Zoom level used when fetching |
| `n_segments` | `int` | Total segments/pixels (n_tiles for Mapbox, pixels for Google) |
| `boundary_name` | `str` | Area name stored in `runs` |
| `traffic_levels` | `dict\|None` | TomTom only: edge_id → raw float traffic_level |

Returns the new `run_id`.

---

## Visualization

### `plot_edges(gdf, color_col='congestion', zoom=14, tiles='OpenStreetMap') → folium.Map`

Folium map of edges colored by congestion level.

### `plot_comparison(mode='driving', zoom=13, sources=None) → None`

Side-by-side folium maps for each source. `sources` defaults to `['mapbox', 'google']`.
Renders inline in Jupyter notebooks.

---

## Database schema reference

See [`database_schema.md`](database_schema.md) for the full column listing.

Key tables:

| Table | Description |
|---|---|
| `driving.edges` | Road segments — pure OSM data, no traffic columns |
| `runs` | One row per pipeline execution |
| `mapbox_congestion_history` | Mapbox congestion time-series |
| `google_congestion_history` | Google congestion time-series |
| `tomtom_congestion_history` | TomTom congestion + raw `traffic_level` |

---

## Full example — notebook workflow

```python
import sys
sys.path.insert(0, '..')
from scripts.mapbox_traffic import MapboxTraffic
from scripts.traffic_db import TrafficDB

DB       = 'db/sodermalm.duckdb'
BOUNDARY = 'boundaries/sodermalm.geojson'
TILES    = 'tiles'
OUT      = 'output/sodermalm_traffic_mapbox.geojson'

# 1. Fetch tiles
traffic = MapboxTraffic(token=MAPBOX_TOKEN, zoom=14)
gdf = traffic.fetch(BOUNDARY, TILES, OUT)

# 2. Map match
edge_cong = traffic.map_match(DB, gdf)

# 3. Write to DB
with TrafficDB(DB, read_only=False) as db:
    run_id = db.write_congestion(edge_cong, source='mapbox', zoom=14,
                                  n_segments=len(gdf), boundary_name='sodermalm')

# 4. Inspect
with TrafficDB(DB) as db:
    print(db.get_congestion_summary('mapbox'))
    m = db.plot_edges(db.get_edges(source='mapbox'))
    db.plot_comparison(sources=['mapbox', 'google'])
```
