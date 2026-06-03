# Matching Stockholm motor-flow onto OSM edges

How Stockholm city's open **motor-traffic-flow** data is attached to the OSM `driving.edges`
network, producing the `edge_stockholm_flow_match` table. This is a *static historical* enrichment
layer (weekday average daily traffic, 2014–2015), complementary to the live Mapbox/TomTom/Google
congestion layers documented in [matching.md](matching.md).

Pipeline code: [`scripts/stockholm_flow.py`](../scripts/stockholm_flow.py). Run:

```bash
conda run -n osm-traffic-enrichment python scripts/stockholm_flow.py
```

---

## Data

| | source (A) | target (B / network) |
|---|---|---|
| **what** | Stockholm `stockholm_motor_flow` (Trafikflöde Motorfordon, CC0) | OSM `driving.edges` |
| **DB** | `fetching-sweden-data/data/processed/sodermalm.duckdb` | `osm-traffic-enrichment/db/sodermalm.duckdb` |
| **rows** | 976 LineString segments (179 streets) | 4,554 directed edges (forward + reverse twins) |
| **CRS** | EPSG:4326 | EPSG:4326 (matched in EPSG:3006, SWEREF99 TM) |

The flow segments are **undirected single centerlines** — one polyline per street stretch,
digitized in a single (arbitrary) direction, with **no reverse twin**. The flow attributes
(volumes, time bands, heavy share, collection method) live in `stockholm_motor_flow`; see
`fetching-sweden-data/docs/stockholm-api.md` for the column meanings — in particular
`flow_all_vehicles` (per-geometry directional value) vs `total_flow_all_directions` (corridor
aggregate; **never `SUM` across carriageways/twins**).

---

## Direction: OSM → flow

The provider pipeline matches **provider → OSM** (segments snapped onto the OSM graph). Here we go
the other way: **each OSM edge is the source (A), matched onto the flow segments (B)**, using the
same engine — `network_matching.run_match_routes` via [`scripts/route_match.py`](../scripts/route_match.py).

Why this direction: we want, *per OSM edge*, the flow segment it lies on, and we need **every edge
(including both directed twins) to get a score** so we can compare a twin against its partner. With
OSM as the source, every edge produces a `routes_summary` row (matched → `dtw_distance` +
`bearing_diff`, otherwise `NO_MATCH`) and `routes_long` rows (one per flow segment in its route).

---

## Steps

1. **Match** — `run_match_routes(flow, osm)` (OSM = source A, flow = destination B) returns
   **`routes_summary`** (one row per OSM edge: route-level `dtw_distance`, `bearing_diff`,
   `overlap_pct`, or `match_type = NO_MATCH`) and **`routes_long`** (one row per
   *OSM edge × flow segment*: `edge_cover_pct`, `edge_match_dist_avg`, `edge_bearing_diff`, `dest_id`).
2. **Filter twins** (on **`routes_summary`**) — group edges by
   `(osm_id, least(source,target), greatest(source,target))` (the forward + reverse edges of a
   two-way street). Per pair, **drop the edge with the lower route-level quality**
   (`100 · exp(-dtw_distance / 15) · max(0, cos(bearing_diff))`; `NO_MATCH` = 0) — the direction
   running against the flow's digitization. The survivors are the **remaining edges** (one per
   two-way street).
3. **Resolve flow link + edge-to-edge scores** (in **`routes_long`**, only for the remaining edges):
   - `source_flow_id` = the dest flow segment covering the most of the edge (`max edge_cover_pct`).
   - `covering_match` = `edge_cover_pct` (that edge↔flow pair).
   - `quality_match`  = `100 · exp(-edge_match_dist_avg / 15) · max(0, cos(edge_bearing_diff))` (that edge↔flow pair).
4. **Extend** — copy the remaining edge's match (`source_flow_id`, `covering_match`, `quality_match`)
   to its dropped twin so both directed edges carry it: survivor `match_source = 'best'`, twin
   `'extended'`.

### Why the twin filter + extend is necessary

The matcher (`network_matching.graph_dtw`) walks the destination network with **forward arcs only**
— it assumes B is a *directed* network where a source running the other way matches that edge's
**reverse twin**. But the flow segments are **undirected** (no reverse twin), digitized one way. So:

- the OSM twin running **with** the flow's digitized direction matches (e.g. `dtw ≈ 2.6 m`,
  `bearing ≈ 4°`);
- the opposite twin runs ~180° against it, finds no forward-walkable B-edge, and returns
  **`NO_MATCH`** (no `dtw`/`bearing`).

Which twin matches is **arbitrary** — it depends only on how Stockholm digitized that flow line. A
two-way street carries traffic both ways, so step 5 copies the match to the `NO_MATCH` twin; both
directed edges then carry the same flow. (Inspect any edge's match with the
network-matching detail tool: `python scripts/graph_dtw_edge_detail.py --edge-id <id> --osm
/tmp/our_osm.csv --sweden /tmp/our_flow.csv`.)

---

## Output: `edge_stockholm_flow_match`

The table stores the **match only** (edge → flow link + scores); flow attribute values are joined
from `stockholm_motor_flow` on demand.

| column | type | meaning |
|---|---|---|
| `edge_id` | BIGINT | OSM `driving.edges` edge |
| `is_reverse` | BOOLEAN | which directed twin (false = forward) |
| `source_flow_id` | VARCHAR | matched flow segment id (`Trafikflode_Motorfordon.N`) |
| `covering_match` | DOUBLE | % of the OSM edge covered by the chosen flow segment |
| `quality_match` | DOUBLE | 0–100, `100·exp(-dtw/15)·max(0,cos(bearing))` |
| `match_source` | VARCHAR | `best` (matched directly) \| `extended` (copied from twin) |
| `matched_at` | TIMESTAMP | run timestamp |

Joining flow values when needed:

```sql
ATTACH '…/fetching-sweden-data/data/processed/sodermalm.duckdb' AS src (READ_ONLY);
SELECT m.*, f.street_name, f.flow_all_vehicles, f.total_flow_all_directions, f.heavy_vehicle_share
FROM   edge_stockholm_flow_match m
JOIN   src.stockholm_motor_flow f ON f.id = m.source_flow_id;
```

### Coverage (Södermalm)

~2,955 edges enriched (≈65%): **1,887 `best` + 1,068 `extended`**. Arterials (trunk/secondary/
tertiary) ≈100%; `service` roads are low because they aren't in Stockholm's car-network *Bilnät*
layer — that's the main reason ~35% of edges stay unmatched, not a matching failure.

---

## Artifacts

- [`scripts/stockholm_flow.py`](../scripts/stockholm_flow.py) — the pipeline (`match_scored` →
  `resolve_twins` → `write_match`).
- [`notebook/7_stockholm_flow_match.ipynb`](../notebook/7_stockholm_flow_match.ipynb) — step-by-step
  report + map.
- `output/sodermalm_flow_match.html` — interactive map of matched edges by flow.
- `output/edge<N>_detail.html` — per-edge graph-DTW detail (from the network-matching tool).

## Caveats

- **Static, mostly modeled.** Södermalm flow is weekday-average daily traffic for 2014–2015, and
  every segment there is `data_collection_method = 'Skattad trafik'` (estimated, not measured).
- **`total_flow_all_directions` is a corridor aggregate** repeated onto both carriageways of a
  divided road — do not sum it across twins/carriageways. Use `flow_all_vehicles` for a
  per-geometry volume.
- Matching is geometric/directional only; it carries the flow **value** unchanged. Any one-way vs
  two-way value adjustment (e.g. splitting a two-way total per direction) is a separate downstream
  step, not done here.
