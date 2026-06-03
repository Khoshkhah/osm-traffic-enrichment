# Traffic Data Flow — Sources, Steps, and Storage

This document follows the traffic data end-to-end: **what we fetch from each provider,
what raw fields come back, how we transform them, and exactly what we store**. It is the
"how it all fits together" view.

Two companion docs go deeper on specific points:

- [traffic_status.md](traffic_status.md) — the meaning of the four congestion levels and the exact classification thresholds per source.
- [database_schema.md](database_schema.md) — the full column listing of every table.

Everything here is driven by one script: [`pipeline_traffic.py`](../pipeline_traffic.py).

---

## 1. The big picture

We enrich an OpenStreetMap road network with live traffic congestion from **three
independent providers** — Mapbox, Google Maps, and TomTom. They are queried separately,
their results are matched onto the same OSM road edges, and each is stored in its own
time-series table.

```
                       boundary polygon (from the DuckDB)
                                   │
        ┌──────────────────────────┼──────────────────────────┐
        ▼                          ▼                          ▼
     MAPBOX                     GOOGLE                      TOMTOM
   vector tiles            map screenshot               vector tiles
        │                          │                          │
   decode + join            classify pixels            decode features
        │                          │                          │
        └──────────── map-match to OSM road edges ────────────┘
                                   │
                       edge_id → congestion level
                                   │
        ┌──────────────────────────┼──────────────────────────┐
        ▼                          ▼                          ▼
 mapbox_congestion_…      google_congestion_…       tomtom_congestion_…
        └──────────────── + one row in `runs` ────────────────┘
```

Each run is a snapshot in time. Running the pipeline repeatedly (hourly, daily, …)
builds a **history** — every run appends new rows; nothing is overwritten.

> **Important:** the OSM road table (`driving.edges`) holds **no** traffic columns.
> It stays pure OSM network data. Congestion lives only in the per-source history
> tables and is joined back to edges at query time. See [database_schema.md](database_schema.md).

---

## 2. Prerequisites (per run)

| What | Where it comes from |
|---|---|
| Road network DuckDB (`db/<area>.duckdb`) | Built earlier by `pipeline_network.py` |
| Boundary polygon | Read from the `boundary` table inside that DuckDB (no separate file needed) |
| API credentials | `.env` file — `MAPBOX_ACCESS_TOKEN`, `GOOGLE_MAPS_API_KEY`, `TOMTOM_API_KEY` |

If a credential is missing, that source is skipped with a warning — the other sources
still run.

**Run it:**
```bash
python pipeline_traffic.py --config config/<area>_traffic.yaml
# or just one source:
python pipeline_traffic.py --config config/<area>_traffic.yaml --source tomtom
```

---

## 3. Mapbox — vector traffic tiles

**What we ask for:** two Mapbox vector tilesets over the boundary, at zoom 14:

- `mapbox.mapbox-traffic-v1` — carries the congestion class as a text property.
- `mapbox.mapbox-streets-v8` — carries the road geometry the congestion sits on.

**Step by step** ([`scripts/mapbox_traffic.py`](../scripts/mapbox_traffic.py)):

1. Compute the set of Web Mercator tiles (z/x/y) that cover the boundary.
2. Download both the traffic and streets `.mvt` tiles in parallel. Tiles are cached on
   disk (`tiles/traffic/`, `tiles/streets/`) and reused on the next run unless `refresh` is set.
3. Decode each tile. The **traffic** features carry a property `congestion` already
   classified as `low` / `moderate` / `heavy` / `severe`. **No color or speed math on our
   side** — Mapbox does the classification.
4. Spatially join streets ↔ traffic (`sjoin_nearest`, within 20 m) so every street
   segment gets a congestion label (or `no data`).
5. Clip to the boundary, save `output/<area>_traffic_mapbox.geojson`.
6. **Map-match** the street segments onto OSM `driving.edges`. The default is route-based
   graph-DTW (`network_matching`, via `scripts/route_match.py`): each segment is matched to
   a *connected route* of OSM edges, then per edge the **longest-overlap** segment wins, and
   edges covered below a floor (`min_edge_coverage`) stay `no data`. A legacy geometric
   matcher (25 m corridor, ≤45° bearing, ≥40% overlap, most-severe-wins) is still selectable
   with `matcher: geometric`. See [matching.md](matching.md).

**Raw data caught:** the `congestion` text label per traffic feature.
**Stored:** `mapbox_congestion_history` → `(run_id, edge_id, congestion, matched_at)`.

---

## 4. Google Maps — screenshot of the traffic layer

Google does not expose a traffic-tile API like the others, so we **render the map and
read the colors off it**.

**What we ask for:** the Google Maps JavaScript `TrafficLayer`, rendered in a headless
Chromium browser (Playwright) as a single big screenshot at zoom 16. The page is styled
to a white background with white roads and no labels, so the only colored thing on it is
the traffic overlay.

**Step by step** ([`scripts/google_traffic.py`](../scripts/google_traffic.py)):

1. Render the boundary area to one screenshot. If the image would be too large for
   memory, the zoom is automatically downgraded (the *effective* zoom is recorded).
2. **Classify every pixel** by its color: convert to CIE-LAB and measure distance to four
   reference colors (teal-green = `low`, yellow = `moderate`, red = `heavy`,
   dark-red = `severe`). Pixels too far from any reference are dropped. Result is a raster
   where each pixel is 0 (none) or 1–4 (a level).
3. **Match colored pixel-stripes to edges** with a bearing-aware KD-tree: estimate the
   local direction of each traffic stripe, find nearby OSM edge sample points whose bearing
   agrees (within 40°), reject pixels sitting at intersections of two ways, and assign each
   pixel to the closest matching edge.
4. Per edge, the **most severe** pixel level wins.

**Raw data caught:** pixel colors only — there is no numeric speed or ratio from Google.
**Stored:** `google_congestion_history` → `(run_id, edge_id, congestion, matched_at)`.

> Google only paints roads that are *not* free-flowing, so an edge with `no data` from
> Google usually means "flowing normally," not "uncovered." See [traffic_status.md](traffic_status.md).

---

## 5. TomTom — vector flow tiles with a raw ratio

**What we ask for:** TomTom Traffic Flow *relative* tiles in PBF format over the boundary,
at zoom 14 (same z/x/y grid idea as Mapbox).

**Step by step** ([`scripts/tomtom_traffic.py`](../scripts/tomtom_traffic.py)):

1. Compute covering tiles, download the `.pbf` tiles in parallel (cached in `tiles/tomtom/`).
2. Decode the `"Traffic flow"` layer. Each feature carries:
   - `traffic_level` — a **continuous float, 0.0 = blocked … 1.0 = free flow** (current
     speed ÷ free-flow speed).
   - `road_closure` — boolean.
   - `road_type` — string.
3. Classify `traffic_level` into the four levels (and force `severe` on a road closure).
4. Clip to boundary, save `output/<area>_traffic_tomtom.geojson`.
5. **Map-match** to OSM edges with the same route-based matcher as Mapbox (longest-overlap
   wins, coverage gate; `matcher: geometric` for the legacy method). The winning segment's
   raw `traffic_level` is carried through.

**Raw data caught:** `traffic_level` (float), `road_closure`, `road_type`.
**Stored:** `tomtom_congestion_history` → `(run_id, edge_id, traffic_level, congestion, matched_at)`.

> TomTom is the only source where we keep the **raw numeric value**. Because
> `traffic_level` is stored, congestion thresholds can be re-applied later without
> re-fetching anything (example query in [traffic_status.md](traffic_status.md)).

---

## 6. What gets stored — the complete list

After a run, the data lands in **three places**:

### a) Inside the DuckDB (the durable store)

| Table | One row per | Key columns |
|---|---|---|
| `runs` | each source-run | `run_id, boundary_name, source, zoom, fetched_at, n_tiles, n_segments` |
| `mapbox_congestion_history` | matched edge | `run_id, edge_id, congestion, matched_at` |
| `google_congestion_history` | matched edge | `run_id, edge_id, congestion, matched_at` |
| `tomtom_congestion_history` | matched edge | `run_id, edge_id, traffic_level, congestion, matched_at` |

- `runs` is the index — every history row points back to a `run_id` here, which records
  *when*, *which source*, *which zoom*, and *how much* (tiles / segments) was fetched.
- The history tables are **append-only time series**. Re-running never overwrites; it
  adds a new `run_id`. "Latest congestion" = the rows for the newest `run_id` per source.
- `driving.edges` is **not** modified — it has no traffic columns.

### b) On disk as files (cache + exports)

| Path | What | Purpose |
|---|---|---|
| `tiles/traffic/*.mvt`, `tiles/streets/*.mvt` | Mapbox raw tiles | cache (skip re-download) |
| `tiles/tomtom/*.pbf` | TomTom raw tiles | cache |
| `output/<area>_traffic_<source>.geojson` | provider segments + congestion | the matched-source layer |
| `output/<area>_edges_traffic_<source>.geojson` / `.csv` | OSM edges + congestion column | ready-to-view / spreadsheet export |
| `logs/…` | run log | auditing |

(Google takes a screenshot rather than tiles, so it has no tile cache.)

### c) Optionally, the cloud

If `MOTHERDUCK_TOKEN` is set, the DuckDB is synced to MotherDuck at the end of the run
(`scripts/motherduck_sync.py`), so the same tables are available cloud-side.

---

## 7. Side-by-side summary

| | Mapbox | Google Maps | TomTom |
|---|---|---|---|
| How we fetch | Vector tiles (MVT) | Map screenshot (Playwright) | Vector tiles (PBF) |
| Default zoom | 14 | 16 (auto-downgrades) | 14 |
| Congestion comes as | Text property | Pixel color | Float ratio → text |
| Raw value kept? | No | No | **Yes** (`traffic_level`) |
| Map-matching | Route-based graph-DTW (default) | Bearing-aware pixel match | Route-based graph-DTW (default) |
| Stored in | `mapbox_congestion_history` | `google_congestion_history` | `tomtom_congestion_history` |
| Typical edge coverage | ~98% | ~5–15% | ~60–80% |

---

## 8. Reading it back

All of the stored data is queryable through the [`traffic_db`](traffic_db.md) Python
helper:

```python
from scripts.traffic_db import TrafficDB

with TrafficDB('db/<area>.duckdb') as db:
    db.get_runs()                                   # every fetch that has happened
    db.get_congestion_summary('tomtom')             # latest distribution for one source
    db.get_congestion_comparison(sources=['mapbox', 'google', 'tomtom'])
    db.get_congestion_history(road_name='Ringvägen', source='mapbox')
```
