# `traffic_db` — Python Library Reference

`traffic_db.py` is a lightweight Python library for reading and writing the
osm-traffic-enrichment DuckDB database. It wraps common queries into a clean API
and provides Folium visualizations.

---

## Quick start

```python
from traffic_db import TrafficDB

# Read-only (default)
with TrafficDB('db/sodermalm.duckdb') as db:
    edges   = db.get_edges(source='google', congestion='heavy')
    summary = db.get_congestion_summary(source='google')
    m       = db.plot_edges(edges)
```

```python
# From a notebook in the notebook/ subfolder
import sys
sys.path.insert(0, '..')
from traffic_db import TrafficDB
```

```python
# Write congestion results (e.g. from notebook 3b)
with TrafficDB('db/sodermalm.duckdb', read_only=False) as db:
    run_id = db.write_congestion(
        edge_congestion,          # dict: edge_id → 'low'|'moderate'|'heavy'|'severe'
        source='google',
        zoom=16,
        n_segments=total_pixels,
        boundary_name='sodermalm',
    )
```

---

## Module constants

```python
from traffic_db import CONGESTION_COLORS, CONGESTION_ORDER
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

Use as a context manager (recommended) or call `close()` manually.

```python
# Context manager — connection closed automatically
with TrafficDB('db/sodermalm.duckdb') as db:
    ...

# Manual close
db = TrafficDB('db/sodermalm.duckdb')
edges = db.get_edges()
db.close()
```

The `con` property returns the underlying `duckdb.DuckDBPyConnection` for raw SQL:

```python
with TrafficDB('db/sodermalm.duckdb') as db:
    result = db.con.execute("SELECT count(*) FROM driving.edges").fetchone()
```

---

## Schema methods

### `list_tables() → DataFrame`

All tables in the database with schema and column count.

```python
db.list_tables()
# schema           table_name  columns
# driving          edges            23
# driving          nodes             3
# main             runs              7
# main             edge_congestion_history  5
# ...
```

### `migrate_schema()`

Ensures all required congestion columns exist. Safe to call repeatedly.
Requires `read_only=False`.

Creates if missing:
- `driving.edges.congestion_mapbox`, `congestion_mapbox_at`
- `driving.edges.congestion_google`, `congestion_google_at`
- `main.runs` table
- `main.edge_congestion_history` table

---

## Edge queries

### `get_edges(...) → GeoDataFrame`

```python
get_edges(
    mode='driving',      # 'driving' | 'walking' | 'cycling'
    source='mapbox',     # 'mapbox' | 'google' — determines the `congestion` column
    highway=None,        # filter by highway type, e.g. 'primary'
    congestion=None,     # filter by level, e.g. 'heavy'
    name=None,           # partial road name search (case-insensitive)
    limit=None,          # max rows to return
)
```

Returns a GeoDataFrame with columns:
`edge_id, highway, name, oneway, length_m, maxspeed_kmh, cost_s, from_cell, to_cell,
congestion, congestion_mapbox, congestion_google, geometry`

The `congestion` column contains the value from `congestion_{source}`.

**Examples:**

```python
# All driving edges, colored by Mapbox
all_edges = db.get_edges(source='mapbox')

# Heavy congestion from Google
heavy = db.get_edges(source='google', congestion='heavy')

# Primary roads only
primaries = db.get_edges(highway='primary')

# Roads named Ringvägen
ring = db.get_edges(name='Ringvägen')
```

### `get_network_stats(mode='driving') → DataFrame`

Edge count, total km, and average speed/cost per highway type.

```python
db.get_network_stats()
# highway       edges    km  avg_speed_kmh  avg_cost_s
# residential    2819  94.2           30.0         4.0
# service         909  43.4           20.0         8.6
# tertiary        365  17.5           35.0         4.9
```

### `get_nearby_edges(lon, lat, radius_m=500, mode='driving', source='mapbox') → GeoDataFrame`

Edges within `radius_m` metres of a point. Returns edges sorted by distance.

```python
nearby = db.get_nearby_edges(lon=18.065, lat=59.315, radius_m=300)
# edge_id  name  highway  congestion  length_m  dist_m  geometry
```

---

## Congestion queries

### `get_congestion_summary(source='mapbox', mode='driving') → DataFrame`

Edge count, total km, and percentage per congestion level.

```python
db.get_congestion_summary(source='google')
# congestion  edges    km   pct
# no data      4112  157.3  90.3
# low           398   30.4   8.7
# moderate       23    2.0   0.5
# heavy           2    0.0   0.0
# severe         19    1.8   0.4
```

### `get_runs() → DataFrame`

All pipeline runs with source, zoom, timestamp, and number of edges matched.

```python
db.get_runs()
# run_id  source  boundary_name  zoom  fetched_at           n_tiles  n_segments  edges_matched
#      1  mapbox  sodermalm         0  2026-05-22 01:57:06        0        4360           4472
#      2  google  sodermalm        16  2026-05-22 01:57:26        0      106405            442
```

### `get_congestion_history(edge_id=None, road_name=None, source=None, limit=500) → DataFrame`

Time-series congestion records. Provide either `edge_id` or `road_name`.

```python
# History for a specific road, both sources
db.get_congestion_history(road_name='Ringvägen', limit=100)

# Mapbox history for a specific edge
db.get_congestion_history(edge_id=175, source='mapbox')
```

Returns: `run_id, fetched_at, source, edge_id, name, highway, congestion, matched_at`

### `get_congestion_comparison(mode='driving') → GeoDataFrame`

All edges with both `congestion_mapbox` and `congestion_google` plus a human-readable
`agreement` column.

```python
gdf = db.get_congestion_comparison()
gdf['agreement'].value_counts()
# Mapbox=low, Google=no data (normal flow)    3441
# both no data                                  582
# sources disagree                              398
# Google only                                    89
# Mapbox congested, Google=no data               23
# both agree (non-low)                            2
```

---

## Write operations

All write methods require `read_only=False`.

### `write_congestion(edge_congestion, source, zoom, n_segments, boundary_name='') → int`

Write congestion results to DuckDB. Identical behavior to the pipeline.

```python
run_id = db.write_congestion(
    edge_congestion = {175: 'heavy', 436: 'moderate', ...},
    source          = 'google',   # or 'mapbox'
    zoom            = 16,
    n_segments      = 109288,     # total traffic pixels (Google) or segments (Mapbox)
    boundary_name   = 'sodermalm',
)
print(f'Written as run {run_id}')
```

**What it writes:**
1. One row in `runs` — source, zoom, timestamp, n_segments
2. Updates `driving.edges.congestion_{source}` and `congestion_{source}_at` for each matched edge
3. Appends one row per edge to `edge_congestion_history`

Returns the new `run_id`.

### `migrate_schema()`

See [Schema methods](#schema-methods) above.

---

## Visualization

### `plot_edges(gdf, color_col='congestion', zoom=14, tiles='OpenStreetMap') → folium.Map`

Folium map of edges colored by congestion level using `CONGESTION_COLORS`.

```python
edges = db.get_edges(source='google')
m = db.plot_edges(edges)
m  # display in Jupyter
```

```python
# Color by a specific column
m = db.plot_edges(edges, color_col='congestion_mapbox', zoom=15)
```

Datetime columns are automatically dropped before rendering (folium cannot serialize them).

### `plot_comparison(mode='driving', zoom=13)`

Side-by-side Mapbox vs Google folium maps. Renders inline in Jupyter notebooks.

```python
db.plot_comparison(zoom=14)
```

---

## Database schema reference

See [`database_schema.md`](database_schema.md) for the full column listing.

Key tables used by this library:

| Table | Schema | Description |
|---|---|---|
| `edges` | `driving` / `walking` / `cycling` | Road segments with geometry and congestion |
| `runs` | `main` | One row per pipeline execution |
| `edge_congestion_history` | `main` | Time-series: congestion per edge per run per source |

---

## Full example — notebook workflow

```python
import sys
sys.path.insert(0, '..')
from traffic_db import TrafficDB

DB = 'db/sodermalm.duckdb'

# Inspect
with TrafficDB(DB) as db:
    print(db.get_congestion_summary(source='google'))
    print(db.get_runs())

    ring = db.get_edges(name='Ringvägen', source='google')
    m = db.plot_edges(ring)
    display(m)

    db.plot_comparison(zoom=14)

# Write results from notebook 3b (edge_congestion + total come from Step 5/3a)
with TrafficDB(DB, read_only=False) as db:
    run_id = db.write_congestion(
        edge_congestion,
        source='google',
        zoom=ZOOM,
        n_segments=total,
        boundary_name=NAME,
    )
```
