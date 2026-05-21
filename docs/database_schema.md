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
    ├── boundary             Boundary polygon from notebook 0
    ├── visualization_metadata  Map center/zoom for web apps
    ├── runs                 One row per Mapbox fetch execution
    ├── traffic_segments     Raw Mapbox traffic segments per run
    ├── edge_congestion_history  Time-series congestion per edge
    └── boundary_cells       H3 hexagon grid covering the boundary
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
| **`congestion_mapbox`** | **VARCHAR** | **Latest Mapbox congestion** (notebook 4) |
| **`congestion_mapbox_at`** | **TIMESTAMP** | **When Mapbox data was last fetched** |
| **`congestion_google`** | **VARCHAR** | **Latest Google Maps congestion** (notebook 3b) |
| **`congestion_google_at`** | **TIMESTAMP** | **When Google data was last fetched** |

**Congestion values:** `low` · `moderate` · `heavy` · `severe` · `no data`

> **Two sources, independent columns.** Each source writes only its own column.
> Run notebook 4 (Mapbox) and/or notebook 3b (Google) independently — they do not overwrite each other.
> `walking.edges` and `cycling.edges` do not have congestion columns.

**Why Google does not need map matching (notebook 4):**
Mapbox provides road *segments* with different geometry from OSM edges → geometric matching needed.
Google PNG tiles encode congestion as pixel colors → we sample directly along each OSM edge → no matching needed.

**Example queries:**
```sql
-- Compare Mapbox vs Google for the same road
SELECT name, highway, congestion_mapbox, congestion_mapbox_at,
                      congestion_google,  congestion_google_at
FROM driving.edges WHERE name IS NOT NULL LIMIT 10;

-- Roads where sources disagree
SELECT edge_id, name, congestion_mapbox, congestion_google
FROM driving.edges
WHERE congestion_mapbox != 'no data'
  AND congestion_google  != 'no data'
  AND congestion_mapbox  != congestion_google;
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

One row per traffic fetch execution (notebook 3 for Mapbox, notebook 3b for Google).
The `run_id` links all historical tables.

| Column | Type | Description |
|---|---|---|
| `run_id` | INTEGER | Auto-incrementing run identifier |
| `boundary_name` | VARCHAR | Name of the boundary (e.g. `sodermalm`) |
| **`source`** | **VARCHAR** | **`'mapbox'` or `'google'`** |
| `zoom` | INTEGER | Tile zoom level used |
| `fetched_at` | TIMESTAMP | UTC timestamp of the fetch |
| `n_tiles` | INTEGER | Number of tiles downloaded |
| `n_segments` | INTEGER | Number of traffic segments decoded |

**Example query:**
```sql
SELECT run_id, fetched_at, n_segments FROM runs ORDER BY fetched_at;
```

---

## `main.traffic_segments` — raw Mapbox traffic data

All Mapbox Traffic v1 segments decoded from tiles, preserved per run.
Grows by ~100–200 rows with each run.

| Column | Type | Description |
|---|---|---|
| `run_id` | INTEGER | Links to `runs.run_id` |
| `segment_id` | INTEGER | Row index within the run |
| `class` | VARCHAR | Mapbox road class (`street`, `motorway`, `primary`, …) |
| `congestion` | VARCHAR | `low` / `moderate` / `heavy` / `severe` |
| `tile` | VARCHAR | Source tile in `z/x/y` format |
| `geometry` | GEOMETRY | LineString in WGS-84 |
| `fetched_at` | TIMESTAMP | UTC timestamp (same as `runs.fetched_at`) |

**Example query:**
```sql
-- Traffic segments per run
SELECT run_id, fetched_at, count(*) segments, congestion
FROM traffic_segments t JOIN runs r USING (run_id)
GROUP BY 1, 2, 4 ORDER BY 2;
```

---

## `main.edge_congestion_history` — congestion time series

The main historical table. One row per matched edge per run, per source.
Use this to answer: *"how did congestion on road X change over time, and does Mapbox agree with Google?"*

| Column | Type | Description |
|---|---|---|
| `run_id` | INTEGER | Links to `runs.run_id` |
| `edge_id` | INTEGER | Links to `driving.edges.edge_id` |
| **`source`** | **VARCHAR** | **`'mapbox'` or `'google'`** |
| `congestion` | VARCHAR | Congestion value at this run |
| `matched_at` | TIMESTAMP | UTC timestamp of the fetch/match |

**Example queries:**
```sql
-- Congestion history for a specific road
SELECT r.fetched_at, h.edge_id, e.name, h.congestion
FROM edge_congestion_history h
JOIN runs r          ON h.run_id  = r.run_id
JOIN driving.edges e ON h.edge_id = e.edge_id
WHERE e.name = 'Ringvägen'
ORDER BY r.fetched_at;

-- How many edges changed congestion between two runs?
SELECT a.edge_id, a.congestion AS before, b.congestion AS after
FROM edge_congestion_history a
JOIN edge_congestion_history b ON a.edge_id = b.edge_id
WHERE a.run_id = 1 AND b.run_id = 2 AND a.congestion <> b.congestion;
```

---

## `main.boundary_cells` — H3 spatial index

H3 hexagonal cells covering the boundary polygon (from notebook 5).
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

## Entity relationships

```
runs ──────────────────────────────────────────────────────────────────┐
  │  run_id                                                            │
  ├── traffic_segments.run_id                                          │
  └── edge_congestion_history.run_id ── edge_id ── driving.edges       │
                                                          │            │
                               from_cell / to_cell ──────┘            │
                                    │                                  │
                             boundary_cells.h3_id                      │
                                                                       │
driving.edges.source / target ── driving.nodes.node_id                 │
driving.edge_graph.from_edge / to_edge ── driving.edges.edge_id        │
driving.turn_restrictions.from_edge_id / to_edge_id ── driving.edges   │
driving.edges.osm_id ── driving.ways.osm_id ── raw.ways.osm_id ───────┘
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
