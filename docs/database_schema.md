# DuckDB Database Schema

The pipeline produces a single `.duckdb` file (e.g. `db/sodermalm.duckdb`) that contains
all data in one place — routing network, traffic history, spatial index, and raw OSM data.

## Schema overview

```
sodermalm.duckdb
│
├── raw.*            Raw OSM data as imported from the PBF file
│   ├── nodes
│   ├── ways
│   └── relations
│
├── driving.*        Road network for motor vehicles      ← primary routing schema
├── walking.*        Road network for pedestrians
├── cycling.*        Road network for cyclists
│   (each mode has the same table structure)
│   ├── edges          Road segments — the main routing table
│   ├── nodes          Network nodes (intersections)
│   ├── ways           OSM ways before splitting into edges
│   ├── way_nodes      Way → node sequence mapping
│   └── edge_graph     Adjacency list for routing
│   (driving only)
│   └── turn_restrictions
│
└── main.*           Enrichment data added by this pipeline
    ├── boundary                   Boundary polygon from notebook 0
    ├── visualization_metadata     Map center/zoom/timezone for web apps
    ├── runs                       One row per traffic fetch execution (all sources)
    ├── mapbox_congestion_history  Mapbox congestion time-series per edge
    ├── google_congestion_history  Google congestion time-series per edge
    ├── tomtom_congestion_history  TomTom congestion + raw traffic_level per edge
    ├── boundary_cells             H3 hexagon grid covering the boundary
    ├── saved_selections_meta     ─┐ Selection sets saved from the Sensor Selector
    └── saved_selections          ─┘ webpage (scripts/sensor_selector.py)
```

---

## `driving.edges` — the primary routing table

The most important table. Each row is a **directed road segment** (source → target node).
Bidirectional roads produce two rows (forward + reverse, `is_reverse = TRUE`).

| Column | Type | Description |
|---|---|---|
| `edge_id` | INTEGER | Unique edge identifier (auto-assigned) |
| `source` | BIGINT | Start node ID (references `nodes.node_id`) |
| `target` | BIGINT | End node ID (references `nodes.node_id`) |
| `osm_id` | BIGINT | Original OSM way ID |
| `highway` | VARCHAR | OSM road classification (`motorway`, `residential`, …) |
| `name` | VARCHAR | Street name (NULL for unnamed roads) |
| `maxspeed` | VARCHAR | Raw OSM `maxspeed` tag (e.g. `"50"`, `"SE:urban"`) |
| `oneway` | VARCHAR | OSM `oneway` tag value |
| `lanes` | VARCHAR | Number of lanes |
| `surface` | VARCHAR | Road surface type |
| `refs` | BIGINT[] | Array of OSM node IDs that make up this edge |
| `geometry` | GEOMETRY | LineString in WGS-84 (EPSG:4326) |
| `is_reverse` | BOOLEAN | `TRUE` if this is the reverse direction of a bidirectional road |
| `length_m` | FLOAT | Edge length in metres (Haversine) |
| `maxspeed_kmh` | FLOAT | Normalised speed limit in km/h |
| `cost_s` | FLOAT | Travel time in seconds (`length_m / (maxspeed_kmh / 3.6)`) |
| `from_cell` | BIGINT | H3 cell ID of the source node |
| `to_cell` | BIGINT | H3 cell ID of the target node |
| `lca_res` | TINYINT | Lowest Common Ancestor H3 resolution of from/to cells |
**Congestion values:** `low` · `moderate` · `heavy` · `severe` · `no data`

> `driving.edges` contains **no traffic columns**. Congestion is stored exclusively in
> per-source history tables (`mapbox_congestion_history`, etc.) and read via CTEs at query
> time using the `traffic_db` library.
> `walking.edges` and `cycling.edges` also have no traffic columns.

**Example queries (via `traffic_db`):**
```python
with TrafficDB('db/sodermalm.duckdb') as db:
    # Latest congestion per edge for a specific source
    edges = db.get_edges(source='mapbox')

    # Compare two sources
    both = db.get_congestion_comparison(sources=['mapbox', 'google'])
```

**Raw SQL using the CTE pattern:**
```sql
WITH latest_mapbox AS (
    SELECT h.edge_id, h.congestion
    FROM mapbox_congestion_history h
    JOIN runs r ON h.run_id = r.run_id
    WHERE r.run_id = (SELECT max(run_id) FROM runs WHERE source = 'mapbox')
)
SELECT e.name, e.highway, coalesce(l.congestion, 'no data') AS congestion
FROM driving.edges e
LEFT JOIN latest_mapbox l ON e.edge_id = l.edge_id
WHERE e.name IS NOT NULL LIMIT 10;
```

---

## `driving.nodes`

Network nodes — intersections and road endpoints.

| Column | Type | Description |
|---|---|---|
| `node_id` | BIGINT | OSM node ID (positive) or virtual node ID (negative) |
| `geom` | GEOMETRY | Point geometry in WGS-84 |
| `h3_cell` | BIGINT | H3 cell ID at the configured resolution |

---

## `driving.edge_graph`

Adjacency list for routing algorithms. A row `(from_edge, to_edge)` means
you can travel from `from_edge` to `to_edge` — i.e. `from_edge.target == to_edge.source`.

| Column | Type | Description |
|---|---|---|
| `from_edge` | INTEGER | Incoming edge ID |
| `to_edge` | INTEGER | Outgoing edge ID |
| `via_edge` | INTEGER | Same as `to_edge` (compatibility alias) |
| `cost` | FLOAT | Travel cost of `from_edge` in seconds |

**Example query:**
```sql
-- Find all edges reachable from a given edge in one hop
SELECT to_edge FROM driving.edge_graph WHERE from_edge = 42;
```

---

## `driving.turn_restrictions`

Turn restrictions from OSM `restriction` relations (driving only).

| Column | Type | Description |
|---|---|---|
| `restriction_id` | BIGINT | OSM relation ID |
| `restriction_type` | VARCHAR | e.g. `no_left_turn`, `only_straight_on` |
| `via_node` | BIGINT | Junction node where the restriction applies |
| `from_edge_id` | INTEGER | Approaching edge |
| `to_edge_id` | INTEGER | Forbidden (or mandatory) exit edge |

---

## `driving.ways`

OSM ways before they are split into directed edges. Useful for tracing back to the
original OSM data.

| Column | Type | Description |
|---|---|---|
| `osm_id` | BIGINT | OSM way ID |
| `highway` | VARCHAR | Road classification |
| `name` | VARCHAR | Street name |
| `maxspeed` | VARCHAR | Raw speed tag |
| `oneway` | VARCHAR | Oneway tag |
| `lanes` | VARCHAR | Lane count |
| `surface` | VARCHAR | Surface type |
| `access` | VARCHAR | Access restriction tag |
| `tags` | MAP(VARCHAR, VARCHAR) | All other OSM tags |
| `refs` | BIGINT[] | Ordered node IDs of the way |

---

## `raw.*` — raw OSM import

These tables hold the unprocessed OSM data as read from the PBF file.
Useful for debugging or accessing tags not carried into the routing schema.

### `raw.nodes`
| Column | Type | Description |
|---|---|---|
| `osm_id` | BIGINT | OSM node ID |
| `lat` | DOUBLE | Latitude |
| `lon` | DOUBLE | Longitude |
| `tags` | MAP(VARCHAR, VARCHAR) | All OSM tags |

### `raw.ways`
| Column | Type | Description |
|---|---|---|
| `osm_id` | BIGINT | OSM way ID |
| `tags` | MAP(VARCHAR, VARCHAR) | All OSM tags |
| `refs` | BIGINT[] | Ordered node IDs |

### `raw.relations`
| Column | Type | Description |
|---|---|---|
| `osm_id` | BIGINT | OSM relation ID |
| `tags` | MAP(VARCHAR, VARCHAR) | All OSM tags |
| `refs` | BIGINT[] | Member IDs |
| `ref_roles` | VARCHAR[] | Member roles (`from`, `via`, `to`, …) |
| `ref_types` | ENUM[] | Member types (`node`, `way`, `relation`) |

---

## `main.runs` — pipeline execution log

One row per traffic fetch execution (any source).
The `run_id` links to the source-specific history table.

| Column | Type | Description |
|---|---|---|
| `run_id` | INTEGER | Auto-incrementing run identifier |
| `boundary_name` | VARCHAR | Name of the boundary (e.g. `sodermalm`) |
| `source` | VARCHAR | `'mapbox'`, `'google'`, or `'tomtom'` |
| `zoom` | INTEGER | Tile zoom level used |
| `fetched_at` | TIMESTAMP | UTC timestamp of the fetch |
| `n_tiles` | INTEGER | Number of tiles downloaded |
| `n_segments` | INTEGER | Number of traffic segments decoded |

---

## `main.mapbox_congestion_history` — Mapbox time series

| Column | Type | Description |
|---|---|---|
| `run_id` | INTEGER | Links to `runs.run_id` |
| `edge_id` | INTEGER | Links to `driving.edges.edge_id` |
| `congestion` | VARCHAR | `low` / `moderate` / `heavy` / `severe` |
| `matched_at` | TIMESTAMP | UTC timestamp |

---

## `main.google_congestion_history` — Google Maps time series

Same schema as `mapbox_congestion_history`.

---

## `main.tomtom_congestion_history` — TomTom time series

| Column | Type | Description |
|---|---|---|
| `run_id` | INTEGER | Links to `runs.run_id` |
| `edge_id` | INTEGER | Links to `driving.edges.edge_id` |
| `traffic_level` | DOUBLE | Raw TomTom relative flow (0.0 = blocked, 1.0 = free flow) |
| `congestion` | VARCHAR | Classified from `traffic_level` |
| `matched_at` | TIMESTAMP | UTC timestamp |

**Example queries:**
```sql
-- Congestion history for a road (Mapbox)
SELECT r.fetched_at, h.edge_id, e.name, h.congestion
FROM mapbox_congestion_history h
JOIN runs r          ON h.run_id  = r.run_id
JOIN driving.edges e ON h.edge_id = e.edge_id
WHERE e.name = 'Ringvägen'
ORDER BY r.fetched_at;

-- TomTom raw flow for heavy segments
SELECT e.name, h.traffic_level, h.congestion
FROM tomtom_congestion_history h
JOIN driving.edges e ON h.edge_id = e.edge_id
WHERE h.congestion IN ('heavy', 'severe')
ORDER BY h.traffic_level;
```

---

## `main.boundary_cells` — H3 spatial index

H3 hexagonal cells covering the boundary polygon (from notebook 4).
Resolution matches the `h3_resolution` setting in the duckOSM config YAML.

| Column | Type | Description |
|---|---|---|
| `h3_id` | VARCHAR | H3 cell ID string (15 hex characters) |
| `resolution` | INTEGER | H3 resolution (default: 8) |
| `geometry` | GEOMETRY | Hexagon polygon in WGS-84 |

**Link to edges:** `boundary_cells.h3_id` ↔ `driving.edges.from_cell` / `to_cell`

**Example query:**
```sql
-- How many edges and what congestion in each H3 cell?
SELECT bc.h3_id, count(e.edge_id) AS edges,
       mode() WITHIN GROUP (ORDER BY e.congestion) AS dominant_congestion
FROM boundary_cells bc
JOIN driving.edges e ON e.from_cell = bc.h3_id
GROUP BY bc.h3_id
ORDER BY edges DESC;
```

---

## `main.boundary` — boundary polygon

The boundary GeoJSON used to filter the network (from notebook 0).

| Column | Type | Description |
|---|---|---|
| `name` | VARCHAR | Place name |
| `osm_id` | INTEGER | OSM relation ID |
| `osm_type` | VARCHAR | OSM type (`relation`) |
| `geom` | GEOMETRY | Boundary polygon |

---

## `main.visualization_metadata` — per-area metadata for web apps

One row per database. Populated during `pipeline_network.py`: the boundary, centroid,
and zoom come from duckOSM (step [2]); the IANA timezone is added in step [3] via
`timezonefinder` from the centroid coordinates.

| Column | Type | Description |
|---|---|---|
| `boundary_geojson` | JSON | GeoJSON of the boundary polygon |
| `center_lat` | DOUBLE | Latitude of the boundary centroid |
| `center_lon` | DOUBLE | Longitude of the boundary centroid |
| `initial_zoom` | INTEGER | Recommended starting zoom level for the map |
| `timezone` | VARCHAR | IANA timezone name at the centroid (e.g. `Europe/Stockholm`) |

---

## `main.saved_selections_meta` — Saved selection sets metadata

Created and written by the **Sensor Selector webpage** (`scripts/sensor_selector.py` —
a small `http.server` app that serves `scripts/sensor_selector.html`). Contains metadata
and custom explanatory notes for road selection sets saved through the UI.

| Column | Type | Description |
|---|---|---|
| `selection_id` | VARCHAR | Unique selection identifier/name (Primary Key) |
| `description` | VARCHAR | Explanation note of why or how the segments were selected |
| `saved_at` | TIMESTAMP | Timestamp of when the selection was written to DuckDB |

---

## `main.saved_selections` — Saved selection road mappings

Created and written by the **Sensor Selector webpage** alongside `saved_selections_meta`.
Contains normalized mappings of road edge IDs belonging to each saved selection set.

| Column | Type | Description |
|---|---|---|
| `selection_id` | VARCHAR | Links to `saved_selections_meta.selection_id` |
| `edge_id` | INTEGER | Links to `driving.edges.edge_id` |

---

## Entity relationships

```
saved_selections_meta.selection_id ── selection_id ── saved_selections.selection_id
                                                               │
                                         driving.edges ── edge_id

runs ─────────────────────────────────────────────────────────────────────┐
  │  run_id                                                               │
  ├── mapbox_congestion_history.run_id ── edge_id ── driving.edges        │
  ├── google_congestion_history.run_id ── edge_id ── driving.edges        │
  └── tomtom_congestion_history.run_id ── edge_id ── driving.edges        │
                                                          │               │
                               from_cell / to_cell ──────┘               │
                                    │                                     │
                             boundary_cells.h3_id                         │
                                                                          │
driving.edges.source / target ── driving.nodes.node_id                    │
driving.edge_graph.from_edge / to_edge ── driving.edges.edge_id           │
driving.turn_restrictions.from_edge_id / to_edge_id ── driving.edges      │
driving.edges.osm_id ── driving.ways.osm_id ── raw.ways.osm_id ──────────┘
```

---

## Useful DuckDB commands

```sql
-- List all schemas
SHOW ALL TABLES;

-- Describe a table
DESCRIBE driving.edges;

-- Count rows in every table
SELECT table_schema, table_name,
       estimated_size AS rows
FROM duckdb_tables()
ORDER BY table_schema, table_name;

-- Check database file size
SELECT * FROM pragma_database_size();
```
